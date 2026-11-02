"""Apply Ruff formatting followed by the project's standalone-comment spacing."""

import argparse
import subprocess
import sys
from pathlib import Path

from scripts.linting.check_readability import ROOT, SCOPES, add_comment_spacing


def main() -> int:
    """Format Python files, or report required changes without writing them."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Report changes without editing files."
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, help="Files or directories; defaults to project scopes."
    )
    arguments = parser.parse_args()

    # Explicit paths let a focused edit format one file, e.g.
    # `uv run python -m scripts.format_python src/bankeriq_agent_platform/runtime.py`.
    # Without paths, use every authored Python scope. The set removes duplicates
    # when the caller supplies both a directory and a file within that directory.
    paths = arguments.paths or [ROOT / scope for scope in SCOPES]
    files = sorted(
        {file for path in paths for file in (path.rglob("*.py") if path.is_dir() else [path])}
    )
    changed = 0

    for path in files:
        source = path.read_text()
        formatted = _format_source(source, path)

        if source == formatted:
            continue

        changed += 1

        if arguments.check:
            print(f"{path}: formatting required")
        else:
            path.write_text(formatted)
            print(f"{path}: formatted")

    return 1 if arguments.check and changed else 0


def _format_source(source: str, path: Path) -> str:
    """Produce the same canonical format for both writes and read-only checks."""

    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(path)],
        input=source,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )

    # Ruff removes leading blank lines inside loops and parentheses. Apply our
    # comment rule last, e.g. for ...:\n\n    # Explain the loop body.
    return add_comment_spacing(formatted.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
