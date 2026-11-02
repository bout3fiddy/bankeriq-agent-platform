"""Run the local quality gate without cloud access or model calls."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("src", "scripts", "tests")


def main() -> None:
    """Check dependencies and code, then run available behavioural tests."""

    # Run the static gates first and stop on the first failed command. For example,
    # a stale uv.lock fails before tests begin, so passing tests cannot disguise
    # an unreproducible dependency set. sys.executable keeps Python checks inside
    # the interpreter selected by `uv run python scripts/verify.py`.
    commands = [
        ["uv", "lock", "--check"],
        [sys.executable, "-m", "scripts.format_python", "--check"],
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

    # Pytest receives this process's environment. Setting BANKERIQ_TEST_POSTGRES_DSN
    # includes the real local database scenarios; leaving it unset makes those
    # tests report explicit skips. This command creates no database or cloud service.
    if test_files:
        subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, check=True)
    else:
        print("No behavioural tests exist yet; scaffold static checks passed.", flush=True)


if __name__ == "__main__":
    main()
