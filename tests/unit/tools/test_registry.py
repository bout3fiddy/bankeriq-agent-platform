"""Permission and version-selection behaviour for the runtime tool catalogue."""

import pytest

from agent_runtime.tools.definitions import OutputPolicy, ToolDefinition
from agent_runtime.tools.registry import ToolRegistry, ToolUnavailableError


def tool(
    name: str,
    permissions: frozenset[str] = frozenset(),
    version: str = "1",
    output_policy: OutputPolicy = OutputPolicy.VISIBLE,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description=f"Synthetic {name} operation",
        argument_schema={"type": "object", "properties": {}},
        output_policy=output_policy,
        required_permissions=permissions,
    )


def test_discovery_requires_all_permissions_and_preserves_output_policy() -> None:
    public = tool("public")
    restricted = tool(
        "analysis", frozenset({"read", "analyse"}), output_policy=OutputPolicy.RESTRICTED
    )
    registry = ToolRegistry([public, restricted])

    assert registry.discover(frozenset()) == (public,)
    assert registry.discover(frozenset({"read"})) == (public,)
    assert registry.discover(frozenset({"read", "analyse"})) == (restricted, public)
    assert restricted.output_policy is OutputPolicy.RESTRICTED


def test_resolution_rechecks_permissions_after_discovery() -> None:
    definition = tool("analysis", frozenset({"analyse"}))
    registry = ToolRegistry([definition])
    assert registry.discover(frozenset({"analyse"})) == (definition,)
    assert registry.resolve("analysis", "1", frozenset({"analyse"})) == definition

    with pytest.raises(ToolUnavailableError):
        registry.resolve("analysis", "1", frozenset())


def test_unavailable_lookup_does_not_reveal_hidden_definitions() -> None:
    registry = ToolRegistry([tool("private", frozenset({"read"}))])
    messages = []

    for name, version in (("unknown", "1"), ("private", "2"), ("private", "1")):
        with pytest.raises(ToolUnavailableError) as failure:
            registry.resolve(name, version, frozenset())

        messages.append(str(failure.value))

    assert messages == ["tool unavailable"] * 3


def test_version_change_requires_explicit_reselection() -> None:
    registry = ToolRegistry([tool("calculate", version="2")])

    with pytest.raises(ToolUnavailableError):
        registry.resolve("calculate", "1", frozenset())

    assert registry.resolve("calculate", "2", frozenset()).version == "2"


@pytest.mark.parametrize("version", ["1", "2"])
def test_duplicate_names_are_rejected_even_across_versions(version: str) -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolRegistry([tool("calculate"), tool("calculate", version=version)])


def test_registry_snapshots_its_input_collection() -> None:
    definitions = [tool("public")]
    registry = ToolRegistry(definitions)
    definitions.append(tool("later"))
    assert tuple(item.name for item in registry.discover(frozenset())) == ("public",)
    assert ToolRegistry([]).discover(frozenset()) == ()


@pytest.mark.parametrize("name,version", [("", "1"), ("tool", " ")])
def test_registration_rejects_missing_identifiers(name: str, version: str) -> None:
    with pytest.raises(ValueError, match="name and version"):
        ToolRegistry([tool(name, version=version)])


def test_registration_requires_an_object_argument_schema() -> None:
    definition = ToolDefinition("tool", "1", "Synthetic operation", {}, OutputPolicy.VISIBLE)

    with pytest.raises(ValueError, match="schema must describe an object"):
        ToolRegistry([definition])
