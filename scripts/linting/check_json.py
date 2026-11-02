"""Validate repository JSON and enforce a maximum of 100 characters per source line."""

import json
import sys

from scripts.linting.check_line_length import ROOT, source_paths


def findings(source: str) -> list[tuple[int, str]]:
    problems = [
        (number, f"JSON line exceeds 100 characters ({len(line)})")
        for number, line in enumerate(source.splitlines(), 1)
        if len(line) > 100
    ]

    try:
        json.loads(source)
    except json.JSONDecodeError as error:
        problems.append((error.lineno, f"invalid JSON: {error.msg}"))

    return problems


def main() -> None:

    failed = False

    for path in source_paths(ROOT):
        relative = path.relative_to(ROOT)

        if path.suffix != ".json" or not path.is_file():
            continue

        for line, message in findings(path.read_text(encoding="utf-8")):
            print(f"{relative}:{line}: {message}")
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
