"""External request validation keeps authority and execution results out of input bodies."""

import json

import pytest

from agent_runtime.hosting.requests import InvalidInvocation, StartRunRequest, parse_invocation

VALID_START = {
    "action": "start_run",
    "conversation_id": "chat",
    "run_id": "run",
    "expected_conversation_revision": 0,
    "message": "Synthetic prompt",
}


def test_start_request_preserves_client_run_id_and_revision() -> None:
    command = parse_invocation(json.dumps(VALID_START).encode())

    assert isinstance(command, StartRunRequest)
    assert command.run_id == "run"
    assert command.expected_conversation_revision == 0
    assert "Synthetic prompt" not in repr(command)


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"action": "unknown"},
        {**VALID_START, "user_id": "PRIVATE_MARKER"},
        {**VALID_START, "tenant_id": "PRIVATE_MARKER"},
        {**VALID_START, "permissions": ["admin"]},
        {**VALID_START, "tools": []},
        {**VALID_START, "model_provider": "other-provider"},
        {**VALID_START, "hosting_provider": "other-host"},
        {**VALID_START, "expected_conversation_revision": "0"},
        {**VALID_START, "expected_conversation_revision": True},
        {**VALID_START, "expected_conversation_revision": -1},
        {**VALID_START, "message": ""},
        {**VALID_START, "message": "x" * 32_001},
        {**VALID_START, "run_id": ""},
        {
            "action": "resume_run",
            "conversation_id": "chat",
            "run_id": "run",
            "call_id": "call",
            "reason": "job",
            "reference_id": "job-1",
            "output": {"private": True},
        },
        {
            "action": "resume_run",
            "conversation_id": "chat",
            "run_id": "run",
            "call_id": "call",
            "reason": "approval",
            "reference_id": "approval-1",
            "granted": True,
        },
    ],
)
def test_invalid_or_privileged_fields_are_rejected_with_fixed_error(body: object) -> None:
    with pytest.raises(InvalidInvocation) as error:
        parse_invocation(json.dumps(body).encode())

    assert str(error.value) == "invalid invocation"


def test_invalid_json_has_no_raw_parse_error() -> None:
    with pytest.raises(InvalidInvocation, match="^invalid invocation$"):
        parse_invocation(b'{"message": "PRIVATE_MARKER')
