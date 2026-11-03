"""Keep agent policy independent of execution adapters and application composition."""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("package", ("agent_harness", "agent_runtime"))
def test_package_dependency_direction(package: str) -> None:
    """A new adapter import must not silently pull clients into pure harness policy.

    For example, importing agent_runtime.storage from memory selection would make
    the harness depend on its own implementation. Likewise, a runtime prompt
    resolver importing application defaults would prevent another application
    from supplying its own catalog. Check every module, including lazy imports.
    """

    violations = []

    for path in (SOURCE / package).rglob("*.py"):
        module = ".".join(path.relative_to(SOURCE).with_suffix("").parts)
        parent = module.rsplit(".", 1)[0]

        for node in ast.walk(ast.parse(path.read_text())):
            names = []

            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [
                    importlib.util.resolve_name("." * node.level + (node.module or ""), parent)
                    if node.level
                    else node.module or ""
                ]
            else:
                continue

            for name in names:
                imported = name.split(".", 1)[0]
                allowed = (
                    imported in sys.stdlib_module_names | {"agent_harness", "pydantic"}
                    if package == "agent_harness"
                    else imported != "agent_platform"
                )

                if not allowed:
                    violations.append(f"{path.relative_to(SOURCE)}:{node.lineno}: {name}")

    assert not violations, "Invalid layer dependencies:\n" + "\n".join(violations)
