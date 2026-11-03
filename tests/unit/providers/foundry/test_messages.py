"""Provider transcript mapping and permitted-output boundaries."""

import json

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import (
    PendingToolCall,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
    WaitReason,
)
from agent_runtime.providers.foundry.messages import encode_messages


def test_batches_match_out_of_order_results_and_omit_wait_references() -> None:
    messages = encode_messages(
        (
            Message(MessageRole.USER, "Calculate and prepare a report"),
            ToolCall("a", "calculate", {"values": [2, 3]}),
            ToolCall("b", "report", {}),
            PendingToolCall("a", WaitReason.JOB, "PRIVATE_JOB_ID"),
            PendingToolCall("b", WaitReason.APPROVAL, "PRIVATE_APPROVAL_ID"),
            PendingToolCall("b", WaitReason.JOB, "PRIVATE_REPORT_JOB"),
            RestrictedReceipt("b"),
            VisibleResult("a", {"total": 5, "nested": {"values": [2, 3]}}),
            ToolCall("c", "calculate", {"values": [5, 5]}),
            ToolFailure("c", ToolErrorCode.DENIED),
            Message(MessageRole.ASSISTANT, "Report ready"),
        )
    )
    payload = json.loads(json.dumps(messages))

    assert [message["role"] for message in payload] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [call["id"] for call in payload[1]["tool_calls"]] == ["a", "b"]
    assert json.loads(payload[1]["tool_calls"][0]["function"]["arguments"]) == {"values": [2, 3]}
    assert payload[2]["tool_call_id"] == "a"
    assert json.loads(payload[2]["content"]) == {
        "status": "completed",
        "result": {"total": 5, "nested": {"values": [2, 3]}},
    }
    assert payload[3]["tool_call_id"] == "b"
    assert json.loads(payload[3]["content"]) == {"status": "completed", "output": "restricted"}
    assert json.loads(payload[5]["content"]) == {"status": "failed", "code": "denied"}
    assert "PRIVATE_" not in json.dumps(messages)


def test_unanswered_call_from_interrupted_turn_has_explicit_unknown_outcome() -> None:
    messages = encode_messages(
        (
            Message(MessageRole.USER, "First request"),
            ToolCall("a", "report", {}),
            PendingToolCall("a", WaitReason.APPROVAL, "PRIVATE_APPROVAL"),
            Message(MessageRole.USER, "New request after cancellation"),
        )
    )
    payload = json.loads(json.dumps(messages))

    assert payload[2] == {
        "role": "tool",
        "tool_call_id": "a",
        "content": '{"status": "unresolved"}',
    }
    assert payload[3]["content"] == "New request after cancellation"


def test_visible_payload_cannot_override_the_result_envelope() -> None:
    messages = encode_messages(
        (ToolCall("a", "calculate", {}), VisibleResult("a", {"status": "failed", "code": "x"}))
    )
    payload = json.loads(json.dumps(messages))

    assert json.loads(payload[1]["content"]) == {
        "status": "completed",
        "result": {"status": "failed", "code": "x"},
    }
