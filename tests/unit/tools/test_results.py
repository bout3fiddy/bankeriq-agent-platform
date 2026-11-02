"""Tool-result payload validation and separation from delivery metadata."""

from dataclasses import fields
from typing import cast

import pytest

from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    ArtifactReference,
    RestrictedReceipt,
    ToolFailure,
    VisibleResult,
)
from agent_runtime.types.json_value import JsonValue


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
