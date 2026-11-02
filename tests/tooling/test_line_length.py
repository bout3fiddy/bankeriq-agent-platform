"""The shared limit covers prose, configuration and ignored local runtime exports."""

import subprocess
from pathlib import Path

from scripts.linting.check_line_length import findings, source_paths


def test_limit_and_narrow_url_exception() -> None:
    assert findings("x" * 100) == []
    assert findings("x" * 101) == [(1, "line exceeds 100 characters (101)")]
    url = "https://example.com/" + "x" * 100
    assert findings("  " + url, markdown=True) == []
    assert findings("Read " + url, markdown=True)
    assert findings(url)


def test_inventory_includes_runtime_export_and_excludes_generated_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".venv/\ninfra/azure/runtime.json\n")
    (tmp_path / "infra/azure").mkdir(parents=True)
    (tmp_path / "infra/azure/runtime.json").write_text("{}")
    (tmp_path / "uv.lock").write_text("generated")
    (tmp_path / "new.md").write_text("authored")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/generated.json").write_text("{}")
    names = {path.relative_to(tmp_path).as_posix() for path in source_paths(tmp_path)}
    assert names == {".gitignore", "infra/azure/runtime.json", "new.md"}
