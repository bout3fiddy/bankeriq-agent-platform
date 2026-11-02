"""Enforce 100-character source lines across repository text and local runtime JSON."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATED_LOCKS = {"uv.lock", "infra/azure/.terraform.lock.hcl"}


def source_paths(root: Path) -> list[Path]:

    # Git inventories authored files without walking caches, environments or
    # build output. runtime.json is explicitly included even though Terraform's
    # local export is ignored; its values are never printed by this checker.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    names = {name.decode() for name in result.stdout.split(b"\0") if name}
    names.add("infra/azure/runtime.json")
    return [root / name for name in sorted(names - GENERATED_LOCKS) if (root / name).is_file()]


def findings(source: str, *, markdown: bool = False) -> list[tuple[int, str]]:
    problems = []

    for number, line in enumerate(source.splitlines(), 1):
        if len(line) <= 100:
            continue

        # A Markdown destination cannot contain a source newline. Keep only
        # isolated reference destinations exempt; prose and link labels still
        # obey the limit. Example: a pinned GitHub source URL on its own line.
        if markdown and re.fullmatch(r"\s*https?://\S+", line):
            continue

        problems.append((number, f"line exceeds 100 characters ({len(line)})"))

    return problems


def main() -> None:
    failed = False

    for path in source_paths(ROOT):
        raw = path.read_bytes()

        if b"\0" in raw:
            continue

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        for number, message in findings(source, markdown=path.suffix == ".md"):
            print(f"{path.relative_to(ROOT)}:{number}: {message}")
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
