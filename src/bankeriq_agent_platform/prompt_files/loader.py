"""Validate pinned JSON components and render deterministic, bounded prompt compositions."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from agent_runtime.prompts.resolution import (
    PromptManifest,
    PromptScope,
    PromptUnavailableError,
    RuntimePrompt,
    compose,
    content_hash,
    validate_body,
)

from .schema import Catalog, Document, Fragment, Reference, Runtime


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
            (catalog.defaults.demo, "fragment"),
            (catalog.defaults.demo_input, "fragment"),
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


def load_catalog(directory: Path | None = None) -> PromptCatalog:
    """Read trusted startup assets; model/user requests cannot select paths or versions."""

    if directory is None:
        source = Path(__file__).resolve().parents[3] / "prompts"
        directory = (
            source
            if (source / "catalog.json").is_file()
            else Path(str(files("bankeriq_agent_platform").joinpath("prompt_data")))
        )

    root = directory.resolve()
    catalog_path = (root / "catalog.json").resolve()

    if not catalog_path.is_relative_to(root):
        raise ValueError("catalog path escapes the configured directory")

    catalog = Catalog.model_validate(_read_json(catalog_path))
    documents: dict[tuple[str, str], Document] = {}

    for pin in catalog.documents:
        relative = PurePosixPath(pin.path)
        target = root.joinpath(*relative.parts).resolve()

        # Reject ../, absolute paths and symlinks that escape the configured
        # directory. A component can only contain JSON; no includes fetch URLs,
        # evaluate templates, expand environment values or read arbitrary files.
        if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
            raise ValueError("prompt path escapes the configured directory")

        raw = _read_bounded(target)

        if len(raw) > 131072 or content_hash(raw.decode("utf-8")) != pin.sha256:
            raise ValueError("prompt file exceeds its bound or does not match its pinned hash")

        document = Document.model_validate(_parse_json(raw))
        key = (pin.id, pin.version)

        if key in documents or (document.prompt.id, document.prompt.version) != key:
            raise ValueError("duplicate or mismatched prompt identity")

        documents[key] = document

    return PromptCatalog(catalog, documents)


def _read_json(path: Path) -> Any:
    return _parse_json(_read_bounded(path))


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as source:
        raw = source.read(131073)

    if len(raw) > 131072:
        raise ValueError("prompt file exceeds its bound")

    return raw


def _parse_json(raw: bytes) -> Any:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key in prompt file")

            result[key] = value

        return result

    return json.loads(raw, object_pairs_hook=unique_keys)
