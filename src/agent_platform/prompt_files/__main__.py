"""Offline authoring: validate, render, or register a new immutable JSON version."""

import argparse
import json
from pathlib import Path, PurePosixPath

from agent_harness.prompts.catalog import PromptCatalog
from agent_harness.prompts.resolution import content_hash
from agent_harness.prompts.schema import Catalog, Document, Pin
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.prompts.files import _read_bounded, _read_json


def register(directory: Path, relative: str) -> None:
    """Pin one new document; existing identities must be retained byte-for-byte.

    For example, copy evidence/1.0.0.json to evidence/1.1.0.json, update its
    version and prose, then register that path. Register dependencies before a
    composition that includes them. Validation finishes before catalog.json is
    replaced, so an unresolved reference leaves the previous catalog intact.
    """

    root = directory.resolve()
    current = load_catalog(root)
    target = (root / relative).resolve()

    if (
        PurePosixPath(relative).is_absolute()
        or ".." in PurePosixPath(relative).parts
        or not target.is_relative_to(root)
    ):
        raise ValueError("prompt path escapes the configured directory")

    raw = _read_bounded(target)
    document = Document.model_validate(_read_json(target)).prompt
    pin = Pin(
        id=document.id,
        version=document.version,
        path=relative,
        sha256=content_hash(raw.decode("utf-8")),
    )

    if any(
        (p.id, p.version) == (pin.id, pin.version) or p.path == pin.path
        for p in current.catalog.documents
    ):
        raise ValueError("publish a new version and path; existing pins are immutable")

    updated = Catalog.model_validate(
        {**current.catalog.model_dump(), "documents": (*current.catalog.documents, pin)}
    )

    # Reconstruct the candidate in memory; no partially valid catalog reaches disk.
    documents = {
        (p.id, p.version): Document.model_validate(_read_json(root / p.path))
        for p in updated.documents
    }
    PromptCatalog(updated, documents)
    temporary = root / "catalog.json.tmp"
    temporary.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(root / "catalog.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render", "add"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--directory", type=Path, default=Path("prompts"))
    args = parser.parse_args()

    if args.command == "add":
        if args.path is None:
            parser.error("add requires a relative JSON path")

        register(args.directory, args.path)

    catalog = load_catalog(args.directory)
    resolver = catalog.resolver()

    if args.command == "render":
        print(resolver.instructions(resolver.resolve()))
    else:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "documents": len(catalog.catalog.documents),
                    "system_version": resolver.resolve().version,
                    "runtime_version": resolver.resolve().runtime_version,
                }
            )
        )


if __name__ == "__main__":
    main()
