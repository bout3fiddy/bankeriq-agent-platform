"""Compile immutable prompt documents without reading files or opening clients."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agent_harness.prompts.resolution import (
    PromptManifest,
    PromptScope,
    PromptUnavailableError,
    RuntimePrompt,
    compose,
    content_hash,
    validate_body,
)
from agent_harness.prompts.schema import Catalog, Document, Fragment, Reference, Runtime


@dataclass(frozen=True, slots=True)
class Component:
    id: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CompiledPrompt:
    """Rendered text plus the ordered source identities needed to audit its publication."""

    reference: Reference
    kind: str
    body: str = field(repr=False)
    components: tuple[Component, ...]
    renderer: str

    def provenance(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "root": self.reference.model_dump(),
            "renderer": self.renderer,
            "content_hash": content_hash(self.body),
            "components": [
                {"id": part.id, "version": part.version, "sha256": part.sha256}
                for part in self.components
            ],
        }


class PromptCatalog:
    """A startup snapshot: later file edits cannot mutate a running request's instructions."""

    def __init__(self, catalog: Catalog, documents: dict[tuple[str, str], Document]) -> None:
        self.catalog = catalog
        self._documents: Mapping[tuple[str, str], Document] = MappingProxyType(dict(documents))
        self._pins = {(pin.id, pin.version): pin for pin in catalog.documents}
        runtimes = [doc.prompt for doc in documents.values() if isinstance(doc.prompt, Runtime)]
        self._runtimes = {runtime.runtime_version: runtime for runtime in runtimes}

        if len(self._runtimes) != len(runtimes):
            raise ValueError("runtime version identities must be unique")

        # Validate every retained version, not just the current defaults. For
        # example, a bankeriq.runtime@1.0.0 checkpoint must still find its pinned body
        # after the default moves to bankeriq.runtime@2.0.0.
        for document in documents.values():
            self.compile(Reference(id=document.prompt.id, version=document.prompt.version))

        for reference, kind in (
            (catalog.defaults.runtime, "runtime"),
            (catalog.defaults.system, "system"),
        ):
            if self.compile(reference).kind != kind:
                raise ValueError("catalog default has the wrong prompt kind")

    def compile(self, reference: Reference) -> CompiledPrompt:
        """Expand exact references in listed order; reject cycles and repeated components."""

        components: list[Component] = []
        seen: set[tuple[str, str]] = set()

        def render(selected: Reference, depth: int) -> str:
            key = (selected.id, selected.version)

            # A -> B -> A and A -> [B, B] both fail explicitly. Silently dropping
            # duplicate instructions could alter their ordering or precedence.
            if key in seen or depth > 12 or len(components) >= 64:
                raise ValueError("prompt graph repeats a component or exceeds its bound")

            document = self._documents.get(key)

            if document is None:
                raise ValueError("prompt references an unavailable component version")

            seen.add(key)
            pin = self._pins[key]
            components.append(Component(pin.id, pin.version, pin.sha256))
            prompt = document.prompt

            if isinstance(prompt, Fragment):
                return "\n\n".join(
                    paragraph if isinstance(paragraph, str) else " ".join(paragraph)
                    for paragraph in prompt.paragraphs
                )

            sections: list[str] = []

            for child in prompt.includes:
                child_document = self._documents.get((child.id, child.version))

                if child_document is None or child_document.prompt.kind not in (
                    "fragment",
                    "section",
                ):
                    raise ValueError("compositions may include only sections and fragments")

                body = render(child, depth + 1)
                sections.append(
                    f"{child_document.prompt.title}\n{body}"
                    if isinstance(child_document.prompt, Fragment)
                    else body
                )

            return f"{prompt.title}\n\n" + "\n\n".join(sections)

        body = render(reference, 0)
        validate_body(body)
        prompt = self._documents[(reference.id, reference.version)].prompt
        renderer = (
            prompt.renderer
            if isinstance(prompt, Runtime)
            else ("verbatim-v1" if isinstance(prompt, Fragment) else "sections-v1")
        )
        return CompiledPrompt(reference, prompt.kind, body, tuple(components), renderer)

    def runtime(self, version: str | None = None) -> RuntimePrompt:
        """Resolve a retained runtime identity, including versions used by older checkpoints."""

        if version is None:
            reference = self.catalog.defaults.runtime
            document = self._documents[(reference.id, reference.version)].prompt
        else:
            document = self._runtimes.get(version)

        if not isinstance(document, Runtime):
            raise PromptUnavailableError("runtime prompt version is unavailable")

        body = self.compile(Reference(id=document.id, version=document.version)).body
        return RuntimePrompt(document.runtime_version, body, document.composer_version)

    def resolver(self) -> CatalogPromptResolver:
        return CatalogPromptResolver(self)


class CatalogPromptResolver:
    """Use the pinned runtime/system defaults for a local profile without database publication."""

    def __init__(self, catalog: PromptCatalog) -> None:
        self._catalog = catalog
        system = catalog.compile(catalog.catalog.defaults.system)
        runtime = catalog.runtime()
        self._manifest = PromptManifest(
            PromptScope("bankeriq", "files"),
            f"{system.reference.id}@{system.reference.version}",
            content_hash(system.body),
            0,
            runtime.version,
            content_hash(runtime.body),
            runtime.composer_version,
        )
        self._body = system.body

    def resolve(self) -> PromptManifest:
        return self._manifest

    def instructions(self, manifest: PromptManifest) -> str:
        if manifest != self._manifest:
            raise PromptUnavailableError(
                "file profile cannot substitute a different prompt manifest"
            )

        return compose(manifest, self._body, self._catalog.runtime(manifest.runtime_version))
