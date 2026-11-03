"""Immutable snapshots of tool calls and registered definitions."""

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from agent_harness.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_harness.types.json_value import JsonValue


def test_call_snapshots_nested_arguments() -> None:
    values: list[JsonValue] = [1, {"region": "north"}]
    arguments: dict[str, JsonValue] = {"values": values}
    call = ToolCall("call-1", "calculate", arguments)
    values.append(2)
    arguments["extra"] = "later"

    assert call.arguments == {"values": (1, {"region": "north"})}

    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], call.arguments)["extra"] = True

    with pytest.raises(FrozenInstanceError):
        setattr(call, "tool_name", "another_tool")  # noqa: B010 - exercise runtime immutability


def test_definition_snapshots_policy_and_schema() -> None:
    schema: dict[str, JsonValue] = {"type": "object", "required": ["count"]}
    definition = ToolDefinition(
        "calculate", "1", "Calculate a public total", schema, OutputPolicy.VISIBLE
    )
    schema["type"] = "string"

    assert definition.argument_schema["type"] == "object"
    assert definition.argument_schema["required"] == ("count",)
