"""Operational logs expose lifecycle metadata and omit application payloads."""

import json
import logging

import pytest

from agent_harness.runs.context import RunContext
from agent_platform.identity.entra import operator_test_input, synthetic_input
from agent_runtime.telemetry import emit


def test_lifecycle_metadata_omits_user_identity_and_unknown_queue(caplog) -> None:
    caplog.set_level(logging.INFO, logger="bankeriq.lifecycle")
    context = RunContext("private-tenant", "private-user", "chat-1", "run-1")
    emit("worker.started", context, queue="PRIVATE_PAYLOAD", attempt=1)
    record = json.loads(caplog.records[-1].message)
    assert record["queue"] == "other" and record["conversation"] == "chat-1"
    assert record["run"] == "run-1" and record["attempt"] == 1
    assert "PRIVATE_PAYLOAD" not in caplog.text
    assert "private-user" not in caplog.text and "private-tenant" not in caplog.text


def test_operator_input_is_separate_opt_in_policy() -> None:
    text = "Remember the synthetic codeword blue-lantern."
    assert operator_test_input(None, text) == text

    with pytest.raises(ValueError):
        synthetic_input(None, text)

    for invalid in ("", "  ", "a\x00b", "a" * 8193):
        with pytest.raises(ValueError):
            operator_test_input(None, invalid)
