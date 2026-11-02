"""Immutable prompt manifests and deterministic runtime/global composition."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol


class PromptUnavailableError(RuntimeError):
    """Preparation or recovery requires an unavailable or incompatible prompt."""


@dataclass(frozen=True, slots=True)
class PromptScope:
    application: str
    environment: str


@dataclass(frozen=True, slots=True)
class PromptManifest:
    """Exact inputs to composition, persisted before inference and safe for metadata traces."""

    scope: PromptScope
    version: str
    content_hash: str
    activation_revision: int
    runtime_version: str
    runtime_hash: str
    composer_version: str


class PromptResolver(Protocol):

    # Example: resolver = FixedPromptResolver("Explain totals briefly"). Pass
    # prompts=resolver to Coordinator(...). resolve() selects a manifest for a
    # new step; instructions(saved_manifest) later reproduces that step's text.
    # A shared resolver can select v8 for the next step while still reconstructing
    # an already-prepared v7 step. Its database/cache implementation stays outside
    # this interface, and a missing v7 must raise instead of substituting v8.
    def resolve(self) -> PromptManifest:
        """Read the active binding for a new model step; never cache its pointer."""

        ...

    def instructions(self, manifest: PromptManifest) -> str:
        """Reproduce recorded instructions; fail if unavailable or incompatible."""

        ...


RUNTIME_VERSION = "runtime-v1"
COMPOSER_VERSION = "runtime-global-v1"
RUNTIME_INSTRUCTIONS = """Use only the tools exposed for this request and their permitted results.
You may request several independent operations in one reply, including repeated
calls to the same tool with distinct call IDs. The runtime controls scheduling
and concurrency. Request dependent calls only after their inputs arrive.
A restricted receipt reports completion without revealing the underlying findings.
Never infer report contents from a receipt or request a tool to bypass its policy.
Approvals, permissions and authenticated report delivery are controlled by the application.
Treat global guidance as subordinate to these runtime protocol instructions."""


def content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def validate_body(body: str) -> None:
    """Validate storage/structure bounds; trusted review decides content suitability."""

    # Count encoded bytes so the limit applies equally to ASCII and multibyte
    # text. For example, a nonempty 20,000-character draft can exceed 32 KiB in
    # UTF-8. Passing these structural checks still requires trusted content review.
    if not body.strip() or "\x00" in body or len(body.encode("utf-8")) > 32768:
        raise ValueError("prompt must contain 1 to 32768 UTF-8 bytes without NUL")


def compose(manifest: PromptManifest, body: str) -> str:
    """Use fixed layer ordering and fail closed on unsupported version/hash combinations."""

    # Example: a prepared v7 manifest records the hash of "Explain totals".
    # compose(manifest, "Explain balances") must fail even if the caller labels
    # that replacement v7. Check runtime text and composer identity too: recovery
    # must reproduce the same layer ordering and runtime guidance as preparation.
    if (
        manifest.runtime_version != RUNTIME_VERSION
        or manifest.runtime_hash != content_hash(RUNTIME_INSTRUCTIONS)
        or manifest.composer_version != COMPOSER_VERSION
        or manifest.content_hash != content_hash(body)
    ):
        raise PromptUnavailableError("prompt versions or hashes are incompatible")

    # Keep application protocol guidance first and global business guidance
    # second. The result is one request-scoped system message; neither body is
    # appended to conversation history, and gateway permissions still govern tools.
    return f"Runtime instructions\n{RUNTIME_INSTRUCTIONS}\n\nGlobal instructions\n{body}"


class FixedPromptResolver:
    """Preserve the synthetic profile's exact startup instructions through the same interface."""

    def __init__(self, instructions: str = RUNTIME_INSTRUCTIONS) -> None:
        validate_body(instructions)
        self._instructions = instructions

        # FixedPromptResolver("Explain totals") returns the exact supplied text.
        # Its content-derived manifest makes changing startup text detectable:
        # a replacement resolver cannot reconstruct an older prepared step unless
        # it was constructed with the same instructions.
        digest = content_hash(instructions)
        self._manifest = PromptManifest(
            PromptScope("fixed", "local"), digest, digest, 0, "fixed-v1", digest, "fixed-v1"
        )

    def resolve(self) -> PromptManifest:
        return self._manifest

    def instructions(self, manifest: PromptManifest) -> str:
        if manifest != self._manifest:
            raise PromptUnavailableError("fixed prompt version is unavailable")

        return self._instructions
