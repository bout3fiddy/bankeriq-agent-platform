"""Enforce paragraph boundaries; adapted from MONKI's Python readability check."""

import ast
import tokenize
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCOPES = ("src", "scripts", "tests")
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)


def findings(source: str) -> list[tuple[int, str]]:
    """Require space between logical stages in authored Python source."""

    tree = ast.parse(source)
    lines = StringIO(source).readlines()
    problems = _comment_spacing_findings(source)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
        ):
            problems.append((node.lineno, "do not import future annotations"))

        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)

            if not isinstance(statements, list):
                continue

            for before, after in zip(statements, statements[1:], strict=False):
                if not isinstance(before, ast.stmt) or not isinstance(after, ast.stmt):
                    continue

                docstring = (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and before is statements[0]
                    and isinstance(before, ast.Expr)
                    and isinstance(before.value, ast.Constant)
                    and isinstance(before.value.value, str)
                )
                separate = docstring or isinstance(before, COMPOUND) or isinstance(after, COMPOUND)

                if separate and not any(
                    not line.strip() for line in lines[before.end_lineno : after.lineno - 1]
                ):
                    problems.append((after.lineno, "add a blank line between logical stages"))

    return sorted(set(problems))


def add_comment_spacing(source: str) -> str:
    """Insert the missing blank lines while preserving comment and string contents."""

    lines = StringIO(source).readlines()
    line_ending = "\r\n" if "\r\n" in source else "\n"

    # Work from the bottom so each diagnostic still points to its original line.
    for line_number, _ in reversed(_comment_spacing_findings(source)):
        lines.insert(line_number - 1, line_ending)

    return "".join(lines)


def _comment_spacing_findings(source: str) -> list[tuple[int, str]]:
    """Require a blank line before the first line of each standalone comment block."""

    lines = StringIO(source).readlines()

    # Tokenization identifies actual comments even when a multiline string
    # contains text such as "# report heading". Inline comments have code before #.
    comment_lines = {
        token.start[0]
        for token in tokenize.generate_tokens(StringIO(source).readline)
        if token.type == tokenize.COMMENT and not token.line[: token.start[1]].strip()
    }
    problems = []

    for line_number in sorted(comment_lines):
        if line_number == 1:
            continue

        # Adjacent comment-only lines form one paragraph, including a bare # line.
        if line_number - 1 in comment_lines:
            continue

        if lines[line_number - 2].strip():
            problems.append((line_number, "add a blank line before a standalone comment block"))

    return problems


def main() -> int:
    """Check all authored Python files in the configured scopes."""

    failures = 0

    for scope in SCOPES:
        for path in sorted((ROOT / scope).rglob("*.py")):
            for line, message in findings(path.read_text()):
                print(f"{path.relative_to(ROOT)}:{line}: {message}")
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
