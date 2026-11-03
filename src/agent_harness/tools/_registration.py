"""Registration checks for tool catalogue entries."""

from agent_harness.tools.definitions import ToolDefinition


def _validate_registration(definition: ToolDefinition) -> None:
    """Enforce catalogue naming and top-level schema requirements at registration."""

    # Registration is the boundary for trusted configuration. Complete schema
    # validation and provider-specific naming rules belong to later integrations.
    if not definition.name.strip() or not definition.version.strip():
        raise ValueError("tool name and version must be nonempty")

    if definition.argument_schema.get("type") != "object":
        raise ValueError("tool argument schema must describe an object")
