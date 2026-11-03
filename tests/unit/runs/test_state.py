"""Run-state invariants for waiting and claimed continuations."""

import pytest

from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunState, RunStatus
from agent_harness.tools.results import PendingToolCall, WaitReason


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
