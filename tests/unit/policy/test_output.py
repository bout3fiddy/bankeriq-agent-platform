"""Synthetic checks of model-visible output and separate artifact delivery."""

from collections.abc import Mapping
from typing import cast

import pytest

from agent_runtime.policy.output import enforce_failure, enforce_output
from agent_runtime.tools.definitions import OutputPolicy, ToolDefinition
from agent_runtime.tools.results import (
    ArtifactReference,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
)
from agent_runtime.types.json_value import JsonValue


def definition(policy: OutputPolicy) -> ToolDefinition:
    return ToolDefinition("calculate", "1", "Synthetic calculation", {"type": "object"}, policy)


def public_total(payload: JsonValue) -> JsonValue:
    values = cast(Mapping[str, JsonValue], payload)
    return {"total": values["total"]}


def test_visible_filter_projects_only_approved_fields() -> None:
    result = enforce_output(
        definition(OutputPolicy.VISIBLE),
        "call-1",
        {"total": 42, "private_notes": "PRIVATE_MARKER"},
        visible_filter=public_total,
        artifact_id="artifact-1",
    )
    assert isinstance(result.model_result, VisibleResult)
    assert result.model_result.payload == {"total": 42}
    assert result.artifact == ArtifactReference("call-1", "artifact-1")


def test_restricted_payload_never_reaches_visible_filter() -> None:
    def forbidden_filter(payload: JsonValue) -> JsonValue:
        pytest.fail("restricted payload reached visible filter")

    result = enforce_output(
        definition(OutputPolicy.RESTRICTED),
        "call-1",
        {"private_notes": "PRIVATE_MARKER", "output_policy": "visible", "call_id": "forged"},
        visible_filter=forbidden_filter,
        artifact_id="artifact-1",
    )
    assert result.model_result == RestrictedReceipt("call-1")
    assert result.artifact == ArtifactReference("call-1", "artifact-1")
    assert "PRIVATE_MARKER" not in repr(result)


def test_restricted_completion_can_have_no_artifact() -> None:
    result = enforce_output(definition(OutputPolicy.RESTRICTED), "call-1", None)
    assert result.model_result == RestrictedReceipt("call-1")
    assert result.artifact is None


@pytest.mark.parametrize("policy", [OutputPolicy.VISIBLE, cast(OutputPolicy, "unknown")])
def test_missing_output_configuration_fails_closed(policy: OutputPolicy) -> None:
    result = enforce_output(definition(policy), "call-1", {"private": "PRIVATE_MARKER"})
    assert result == enforce_failure("call-1")


def test_filter_exception_details_are_suppressed() -> None:
    def failing_filter(payload: JsonValue) -> JsonValue:
        raise RuntimeError("PRIVATE_MARKER in worker result")

    result = enforce_output(
        definition(OutputPolicy.VISIBLE), "call-1", None, visible_filter=failing_filter
    )
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.EXECUTION_FAILED)
    assert result.artifact is None
    assert "PRIVATE_MARKER" not in repr(result)


def test_invalid_filter_output_becomes_a_fixed_failure() -> None:
    result = enforce_output(
        definition(OutputPolicy.VISIBLE),
        "call-1",
        float("nan"),
        visible_filter=lambda payload: payload,
    )
    assert result == enforce_failure("call-1")


def test_visible_result_is_snapshotted_after_filtering() -> None:
    values: list[JsonValue] = [1, 2]
    result = enforce_output(
        definition(OutputPolicy.VISIBLE), "call-1", values, visible_filter=lambda payload: payload
    )
    values.append(3)
    assert isinstance(result.model_result, VisibleResult)
    assert result.model_result.payload == (1, 2)
