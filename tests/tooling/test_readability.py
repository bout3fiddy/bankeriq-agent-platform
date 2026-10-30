"""Comment-block spacing across Python statements, strings and file headers."""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.linting.check_readability import ROOT, add_comment_spacing, findings


@pytest.mark.parametrize(
    "source,expected_lines",
    [
        ("count = 2\n# Explain count.\nresult = count\n", [2]),
        ("count = 2\n# First line.\n#\n# Last line.\nresult = count\n", [2]),
        ("count = 2\n\n# Explain count.\n# Another line.\nresult = count\n", []),
        ("count = 2\n   \n# Explain count.\nresult = count\n", []),
        ("# File header.\n# Another line.\ncount = 2\n", []),
        ("#!/usr/bin/env python\n# coding: utf-8\ncount = 2\n", []),
        ("count = 2  # Default count.\n", []),
        ("count = 2  # Default count.\n# New block.\nresult = count\n", [2]),
        ('text = """Report\n# Report heading\n"""\n', []),
        ("text = '\u2028'\n# Actual comment.\nresult = text\n", [2]),
        ('text = """Report\n# Report heading"""\n# Actual comment.\nresult = text\n', [3]),
        ("values = (\n    # First value.\n    2,\n)\n", [2]),
        ("values = (\n\n    # First value.\n    2,\n)\n", []),
        ("def total():\n    # Explain return.\n    return 2\n", [2]),
        ("for value in (2,):\n    # Explain body.\n    print(value)\n", [2]),
        ("count = 2\n# First block.\nresult = count\n# Second block.\nprint(result)\n", [2, 4]),
    ],
)
def test_standalone_comment_spacing(source: str, expected_lines: list[int]) -> None:
    comment_findings = [
        line
        for line, message in findings(source)
        if message == "add a blank line before a standalone comment block"
    ]
    assert comment_findings == expected_lines
    formatted = add_comment_spacing(source)
    assert findings(formatted) == []
    assert add_comment_spacing(formatted) == formatted

    if not expected_lines:
        assert formatted == source


def test_formatter_preserves_comment_spacing_after_ruff(tmp_path: Path) -> None:
    source = (
        "values=(\n    # First value.\n    2,\n)\n\n"
        "for value in values:\n    # Explain body.\n    print('value',value)\n"
    )
    path = tmp_path / "example.py"
    path.write_text(source)
    command = [sys.executable, "-m", "scripts.format_python"]
    before = subprocess.run(
        [*command, "--check", str(path)], cwd=ROOT, capture_output=True, text=True
    )
    assert before.returncode == 1
    assert path.read_text() == source

    subprocess.run([*command, str(path)], cwd=ROOT, check=True, capture_output=True, text=True)
    formatted = path.read_text()
    assert 'print("value", value)' in formatted
    assert "values = (\n\n    # First value." in formatted
    assert "for value in values:\n\n    # Explain body." in formatted
    assert findings(formatted) == []

    subprocess.run(
        [*command, "--check", str(path)], cwd=ROOT, check=True, capture_output=True, text=True
    )
    assert path.read_text() == formatted
