"""JSON authoring keeps exact sources, bounded composition and startup snapshots."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.prompts.resolution import PromptUnavailableError, content_hash
from bankeriq_agent_platform.prompt_files.__main__ import register
from bankeriq_agent_platform.prompt_files.loader import load_catalog
from bankeriq_agent_platform.prompt_files.schema import Catalog, Document

ROOT = Path(__file__).resolve().parents[3] / "prompts"


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    shutil.copytree(ROOT, tmp_path / "prompts")
    return tmp_path / "prompts"


def write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def change_component(directory: Path, transform) -> None:
    catalog = json.loads((directory / "catalog.json").read_text())
    pin = catalog["documents"][0]
    target = directory / pin["path"]
    data = json.loads(target.read_text())
    transform(data["prompt"])
    write(target, data)
    pin["sha256"] = content_hash(target.read_text())
    write(directory / "catalog.json", catalog)


def test_default_composition_and_snapshot(directory: Path) -> None:
    catalog = load_catalog(directory)
    resolver = catalog.resolver()
    manifest = resolver.resolve()
    body = resolver.instructions(manifest)
    assert manifest.runtime_version == "bankeriq.runtime@1.0.0"
    assert "BankerIQ" in body and "restricted" in body and "permissions" in body
    assert body.startswith(catalog.runtime().body)
    (directory / "catalog.json").unlink()
    assert resolver.instructions(manifest) == body

    with pytest.raises(PromptUnavailableError):
        resolver.instructions(replace(manifest, version="unknown"))

    with pytest.raises(PromptUnavailableError):
        catalog.runtime("unknown")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p.update(unrecognised="value"),
        lambda p: p.update(version="latest"),
    ],
)
def test_invalid_document(directory: Path, mutation) -> None:
    change_component(directory, mutation)

    with pytest.raises(ValueError):
        load_catalog(directory)


@pytest.mark.parametrize("path", ["../outside.json", "/outside.json"])
def test_escaping_paths(directory: Path, path: str) -> None:
    data = json.loads((directory / "catalog.json").read_text())
    data["documents"][0]["path"] = path
    write(directory / "catalog.json", data)

    with pytest.raises(ValueError, match="escapes"):
        load_catalog(directory)


def test_hash_duplicates_size_and_symlinks(directory: Path, tmp_path: Path) -> None:
    data = json.loads((directory / "catalog.json").read_text())
    target = directory / data["documents"][0]["path"]
    original = target.read_bytes()
    target.write_bytes(original + b" ")

    with pytest.raises(ValueError, match="hash"):
        load_catalog(directory)

    target.write_bytes(b"x" * 131073)

    with pytest.raises(ValueError, match="bound"):
        load_catalog(directory)

    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        load_catalog(directory)

    (directory / "catalog.json").write_text('{"schema_version":1,"schema_version":1}')

    with pytest.raises(ValueError, match="duplicate JSON"):
        load_catalog(directory)


def test_register_new_version_preserves_previous(directory: Path) -> None:
    data = json.loads((directory / "catalog.json").read_text())
    pin = data["documents"][0]
    source = json.loads((directory / pin["path"]).read_text())
    source["prompt"]["version"] = "1.1.0"
    write(directory / "new.json", source)
    register(directory, "new.json")
    assert len(load_catalog(directory).catalog.documents) == len(data["documents"]) + 1

    with pytest.raises(ValueError, match="immutable"):
        register(directory, "new.json")


@pytest.mark.parametrize("mode", ["missing", "cycle", "duplicate"])
def test_bad_graph(directory: Path, mode: str) -> None:
    data = json.loads((directory / "catalog.json").read_text())
    root = data["defaults"]["system"]
    pin = next(p for p in data["documents"] if p["id"] == root["id"])
    target = directory / pin["path"]
    document = json.loads(target.read_text())
    includes = document["prompt"]["includes"]

    if mode == "missing":
        includes[0]["version"] = "9.9.9"
    elif mode == "cycle":
        document["prompt"]["kind"] = "section"
        includes.append(root)
    else:
        includes.append(includes[0])

    write(target, document)
    pin["sha256"] = content_hash(target.read_text())
    write(directory / "catalog.json", data)

    with pytest.raises(ValueError):
        load_catalog(directory)


def test_exported_schemas_match_models() -> None:
    for name, model in (("catalog", Catalog), ("document", Document)):
        saved = json.loads((ROOT / "schemas" / f"{name}.schema.v1.json").read_text())
        saved.pop("$schema")
        saved.pop("$id")
        assert saved == model.model_json_schema()


def test_legacy_runtime_hash_is_byte_identical() -> None:

    # Hash of runtime-v1 before its move from Python to JSON. A heading or newline
    # change would make old saved model-step manifests unreconstructable.
    assert content_hash(load_catalog().runtime("runtime-v1").body) == (
        "f9fda1b2b88a0412a077880bb840b2f043dd182d36891dee6c97c2546c45c822"
    )


def test_nested_sections_render_once_in_declared_order(directory: Path) -> None:
    catalog = load_catalog(directory)
    root = catalog.catalog.defaults.system
    data = json.loads((directory / "system/1.0.0.json").read_text())
    data["prompt"].update(id="section.example", kind="section", title="Nested guidance")
    write(directory / "section.json", data)
    register(directory, "section.json")
    data["prompt"].update(
        id=root.id,
        version="1.1.0",
        kind="system",
        title="New root",
        includes=[{"id": "section.example", "version": "1.0.0"}],
    )
    write(directory / "system-new.json", data)
    register(directory, "system-new.json")
    from bankeriq_agent_platform.prompt_files.schema import Reference

    body = load_catalog(directory).compile(Reference(id=root.id, version="1.1.0")).body
    assert body.count("Nested guidance") == 1
    assert body.startswith("New root\n\nNested guidance\n\n")
    assert body.endswith(catalog.compile(root).body.split("\n\n", 1)[1])


def test_failed_registration_keeps_catalog_bytes(directory: Path) -> None:
    before = (directory / "catalog.json").read_bytes()
    data = json.loads((directory / "system/1.0.0.json").read_text())
    data["prompt"].update(version="2.0.0", includes=[{"id": "missing", "version": "1.0.0"}])
    write(directory / "unresolved.json", data)

    with pytest.raises(ValueError):
        register(directory, "unresolved.json")

    assert (directory / "catalog.json").read_bytes() == before


def test_application_default_and_explicit_compatibility_override() -> None:
    from bankeriq_agent_platform.configuration import load_settings

    settings = load_settings({"MODEL_NAME": "synthetic"})
    assert settings.prompts is not None
    assert settings.prompts.resolve().version == "bankeriq.system@1.0.0"
    assert settings.model.instructions == settings.prompts.instructions(settings.prompts.resolve())
    explicit = load_settings({"MODEL_NAME": "synthetic", "MODEL_INSTRUCTIONS": "Explicit test"})
    assert explicit.model.instructions == "Explicit test"
