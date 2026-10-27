"""Run the local quality gate without cloud access or model calls."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("src", "scripts", "tests")


def main() -> None:
    """Check dependencies and code, then run available behavioural tests."""

    commands = [
        ["uv", "lock", "--check"],
        [sys.executable, "-m", "ruff", "format", "--check", *SCOPES],
        [sys.executable, "-m", "ruff", "check", *SCOPES],
        [sys.executable, "scripts/linting/check_readability.py"],
        ["uv", "run", "--locked", "basedpyright"],
    ]

    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)

    test_files = [
        path
        for path in (ROOT / "tests").rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    ]

    if test_files:
        subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, check=True)
    else:
        print("No behavioural tests exist yet; scaffold static checks passed.", flush=True)


if __name__ == "__main__":
    main()
