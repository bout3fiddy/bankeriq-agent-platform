"""Read a caller-selected local prompt catalog with bounded paths and bytes."""

import json
from pathlib import Path, PurePosixPath
from typing import Any

from agent_harness.prompts.catalog import PromptCatalog
from agent_harness.prompts.resolution import content_hash
from agent_harness.prompts.schema import Catalog, Document


def load_catalog(directory: Path) -> PromptCatalog:
    """Read trusted startup assets; model/user requests cannot select paths or versions."""

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
