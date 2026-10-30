"""Behavioural invariants for provider-independent runtime values."""

from dataclasses import FrozenInstanceError, fields
from typing import cast

import pytest

from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunState, RunStatus
from agent_runtime.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_runtime.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    ToolFailure,
    VisibleResult,
    WaitReason,
)
from agent_runtime.types.json_value import JsonValue


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1: "bad key"}])
def test_non_json_payloads_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        VisibleResult("call-1", cast(JsonValue, value))


def test_cycles_are_rejected_but_shared_values_are_allowed() -> None:
    cyclic: list[JsonValue] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="cycles"):
        VisibleResult("call-1", cyclic)

    shared: dict[str, JsonValue] = {"total": 3}
    result = VisibleResult("call-2", [shared, shared])
    assert result.payload == ({"total": 3}, {"total": 3})


def test_restricted_receipt_has_no_payload_or_delivery_fields() -> None:
    assert {item.name for item in fields(RestrictedReceipt)} == {"call_id"}
    assert {item.name for item in fields(ToolFailure)} == {"call_id", "code"}
    artifact = ArtifactReference("call-1", "artifact-1")
    assert not isinstance(artifact, RestrictedReceipt)

    with pytest.raises(TypeError):
        RestrictedReceipt(**{"call_id": "call-1", "payload": "private"})


def test_payloads_are_not_in_default_representations() -> None:
    assert "PRIVATE_MARKER" not in repr(VisibleResult("call-1", "PRIVATE_MARKER"))
    assert "PRIVATE_MARKER" not in repr(ToolCall("call-1", "tool", {"x": "PRIVATE_MARKER"}))


def test_waiting_state_requires_unique_pending_calls() -> None:
    context = RunContext("tenant-1", "user-1", "conversation-1", "run-1")
    pending = PendingToolCall("call-1", WaitReason.JOB, "job-1")
    assert RunState(context, RunStatus.WAITING, (pending,)).pending_calls == (pending,)
    assert RunState(context, RunStatus.RUNNING, (pending,)).pending_calls == (pending,)

    for status, calls in (
        (RunStatus.WAITING, ()),
        (RunStatus.COMPLETED, (pending,)),
        (RunStatus.QUEUED, (pending,)),
        (RunStatus.WAITING, (pending, pending)),
    ):
        with pytest.raises(ValueError):
            RunState(context, status, calls)
