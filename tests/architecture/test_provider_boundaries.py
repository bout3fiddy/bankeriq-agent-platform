"""Keep runtime components usable without any vendor SDK or application wiring."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src/agent_runtime"
CORE_FILES = [
    path for path in RUNTIME.rglob("*.py") if "providers" not in path.relative_to(RUNTIME).parts
]
VENDORS = {"azure", "openai", "anthropic", "boto3", "botocore"}


def test_core_imports_point_inwards() -> None:
    violations: list[str] = []

    for path in CORE_FILES:
        package = ".".join(path.parent.relative_to(ROOT / "src").parts)

        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue

            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                name = "." * node.level + (node.module or "")
                module = importlib.util.resolve_name(name, package)
                modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]

            for module in modules:
                if module.split(".")[0] in VENDORS or module.startswith(
                    ("agent_runtime.providers", "bankeriq_agent_platform")
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")

    assert not violations, "\n".join(violations)


def test_core_and_startup_import_without_vendor_sdks() -> None:
    modules = [
        ".".join(path.relative_to(ROOT / "src").with_suffix("").parts) for path in CORE_FILES
    ]

    # A fresh interpreter prevents imports cached by provider tests from hiding
    # an accidental SDK dependency. Startup registration must also remain lazy.
    script = f"""
import importlib
import importlib.abc
import sys

class BlockVendors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {VENDORS!r}:
            raise ImportError('vendor import blocked: ' + fullname)
        return None

sys.meta_path.insert(0, BlockVendors())
for name in {modules!r}:
    importlib.import_module(name)

from bankeriq_agent_platform.providers import hosting_providers, model_providers
from bankeriq_agent_platform.runtime import open_runtime
assert set(model_providers({{}})) == {{'foundry'}}
assert set(hosting_providers()) == {{'foundry'}}
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
