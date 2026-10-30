"""Complete synthetic workflows through the assembled runtime and its policy boundaries."""

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import cast

import pytest

from agent_runtime.coordinator.model import ModelReply, ModelRequest
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
    WaitReason,
)
from agent_runtime.types.json_value import JsonValue

from .harness import (
    CALCULATE,
    CONTEXT,
    PERMISSIONS,
    PRIVATE_BODY,
    REPORT,
    RuntimeHarness,
    event_for,
    inspect_fields,
)

PUBLIC_CALL = ToolCall("sum-1", CALCULATE.name, {"values": [12, 30]})
REPORT_CALL = ToolCall("report-1", REPORT.name, {"dataset": "sample", "total": 42})


@pytest.fixture
def runtime() -> Iterator[RuntimeHarness]:
    harness = RuntimeHarness()
    yield harness

    # Every scenario checks every model request and the final stored history,
    # including failure paths where a payload might otherwise escape unnoticed.
    harness.assert_public_boundaries()


def test_public_result_drives_approved_report_and_authenticated_delivery(
    runtime: RuntimeHarness,
) -> None:
    def request_report(request: ModelRequest) -> ModelReply:
        result = request.history[-1]
        assert isinstance(result, VisibleResult)
        total = cast(Mapping[str, JsonValue], result.payload)["total"]
        return ModelReply(
            calls=(ToolCall("report-1", REPORT.name, {"dataset": "sample", "total": total}),)
        )

    runtime.model.replies = [
        ModelReply(calls=(PUBLIC_CALL,)),
        request_report,
        ModelReply(answer="The total is 42; your report is ready."),
    ]
    waiting = runtime.start()
    assert waiting.run.status is RunStatus.WAITING
    approval = waiting.run.pending_calls[0]
    assert approval.reason is WaitReason.APPROVAL
    assert runtime.executions == [(CALCULATE.name, CONTEXT)]
    assert runtime.model.requests[1].history[-1] == VisibleResult("sum-1", {"total": 42})
    assert runtime.access.approvals[(CONTEXT, REPORT_CALL.call_id)].call == REPORT_CALL

    decision = runtime.events.decide(CONTEXT, approval, granted=True)
    queued = runtime.continuations.resume(CONTEXT, decision)
    assert queued.run.status is RunStatus.WAITING
    job = queued.run.pending_calls[0]
    assert job.reason is WaitReason.JOB
    assert len(runtime.model.requests) == 2

    completed = runtime.continuations.resume(CONTEXT, event_for(job))
    assert completed.run.status is RunStatus.COMPLETED
    assert completed.answer == "The total is 42; your report is ready."
    assert completed.run.model_steps == 3
    assert completed.run.tool_calls == 2
    assert runtime.model.requests[2].history[-1] == RestrictedReceipt(REPORT_CALL.call_id)
    assert runtime.executions == [(CALCULATE.name, CONTEXT), (REPORT.name, CONTEXT)]
    artifact = completed.artifacts[0]
    assert artifact.call_id == REPORT_CALL.call_id
    assert runtime.read_report(CONTEXT, artifact.artifact_id) == f"{PRIVATE_BODY}: total=42"

    for other in (replace(CONTEXT, user_id="other"), replace(CONTEXT, tenant_id="other")):
        with pytest.raises(PermissionError):
            runtime.read_report(other, artifact.artifact_id)

    runtime.access.permissions[(CONTEXT.tenant_id, CONTEXT.user_id)] = PERMISSIONS - {
        "reports.read"
    }

    with pytest.raises(PermissionError):
        runtime.read_report(CONTEXT, artifact.artifact_id)

    saved = runtime.store.load_run(CONTEXT)

    with pytest.raises(StateConflictError):
        runtime.continuations.resume(CONTEXT, event_for(job))

    assert runtime.store.load_run(CONTEXT) == saved
    assert len(runtime.events.submissions) == 1


@pytest.mark.parametrize(
    "reason,code",
    [
        ("permission", ToolErrorCode.UNAVAILABLE),
        ("resource", ToolErrorCode.DENIED),
        ("arguments", ToolErrorCode.INVALID_ARGUMENTS),
    ],
)
def test_rejected_report_has_no_side_effects(
    runtime: RuntimeHarness, reason: str, code: ToolErrorCode
) -> None:
    call = REPORT_CALL

    if reason == "permission":
        runtime.access.permissions[("tenant", "user")] = frozenset({"public.calculate"})
    elif reason == "resource":
        runtime.access.datasets[("tenant", "user")] = frozenset()
    else:
        call = ToolCall("report-1", REPORT.name, {"dataset": "sample", "total": "invalid"})

    runtime.model.replies = [
        ModelReply(calls=(call,)),
        ModelReply(answer="Unable to prepare the report."),
    ]
    result = runtime.start()
    assert result.run.status is RunStatus.COMPLETED
    assert runtime.model.requests[1].history[-1] == ToolFailure(call.call_id, code)
    assert runtime.executions == []
    assert runtime.access.approvals == {}
    assert runtime.events.submissions == []
    assert runtime.reports == {}

    if reason == "permission":
        assert [tool.name for tool in runtime.model.requests[0].tools] == [CALCULATE.name]


def test_denied_approval_returns_failure_without_submitting_job(runtime: RuntimeHarness) -> None:
    runtime.model.replies = [
        ModelReply(calls=(REPORT_CALL,)),
        ModelReply(answer="Approval was declined."),
    ]
    waiting = runtime.start()
    event = runtime.events.decide(CONTEXT, waiting.run.pending_calls[0], granted=False)
    result = runtime.continuations.resume(CONTEXT, event)
    assert result.run.status is RunStatus.COMPLETED
    assert runtime.model.requests[1].history[-1] == ToolFailure(
        REPORT_CALL.call_id, ToolErrorCode.DENIED
    )
    assert runtime.executions == []
    assert runtime.events.submissions == []


def test_permission_revoked_after_discovery_blocks_execution(runtime: RuntimeHarness) -> None:
    def revoke_before_call(request: ModelRequest) -> ModelReply:
        assert CALCULATE.name in [tool.name for tool in request.tools]
        runtime.access.permissions[("tenant", "user")] = frozenset()
        return ModelReply(calls=(PUBLIC_CALL,))

    runtime.model.replies = [revoke_before_call, ModelReply(answer="Access is unavailable.")]
    result = runtime.start()
    assert result.run.status is RunStatus.COMPLETED
    assert runtime.executions == []
    assert runtime.model.requests[1].tools == ()
    assert runtime.model.requests[1].history[-1] == ToolFailure(
        PUBLIC_CALL.call_id, ToolErrorCode.UNAVAILABLE
    )


@pytest.mark.parametrize("stage", [WaitReason.APPROVAL, WaitReason.JOB])
@pytest.mark.parametrize("revoke", ["permission", "resource"])
def test_current_access_is_rechecked_after_wait(
    runtime: RuntimeHarness, stage: WaitReason, revoke: str
) -> None:
    runtime.model.replies = [
        ModelReply(calls=(REPORT_CALL,)),
        ModelReply(answer="Access is unavailable."),
    ]
    waiting = runtime.start()
    event = runtime.events.decide(CONTEXT, waiting.run.pending_calls[0], granted=True)

    if stage is WaitReason.JOB:
        waiting = runtime.continuations.resume(CONTEXT, event)
        event = event_for(waiting.run.pending_calls[0])

    if revoke == "permission":
        runtime.access.permissions[("tenant", "user")] = PERMISSIONS - {"reports.create"}
        expected = ToolErrorCode.UNAVAILABLE
    else:
        runtime.access.datasets[("tenant", "user")] = frozenset()
        expected = ToolErrorCode.DENIED

    result = runtime.continuations.resume(CONTEXT, event)
    assert result.run.status is RunStatus.COMPLETED
    assert result.artifacts == ()
    assert runtime.model.requests[1].history[-1] == ToolFailure(REPORT_CALL.call_id, expected)
    assert len(runtime.executions) == (1 if stage is WaitReason.JOB else 0)


@pytest.mark.parametrize("stage", ["calculation", "filter", "report"])
def test_private_failure_details_stay_out_of_followup_model_request(
    runtime: RuntimeHarness, stage: str
) -> None:
    runtime.fail_calculation = stage == "calculation"
    runtime.fail_filter = stage == "filter"
    runtime.fail_report = stage == "report"
    call = REPORT_CALL if stage == "report" else PUBLIC_CALL
    runtime.model.replies = [ModelReply(calls=(call,)), ModelReply(answer="The operation failed.")]
    result = runtime.start()

    if stage == "report":
        event = runtime.events.decide(CONTEXT, result.run.pending_calls[0], granted=True)
        result = runtime.continuations.resume(CONTEXT, event)

    assert result.run.status is RunStatus.COMPLETED
    assert result.artifacts == ()
    assert runtime.reports == {}
    assert runtime.model.requests[1].history[-1] == ToolFailure(
        call.call_id, ToolErrorCode.EXECUTION_FAILED
    )


def test_model_failure_after_tool_result_preserves_safe_history(runtime: RuntimeHarness) -> None:
    runtime.model.replies = [ModelReply(calls=(PUBLIC_CALL,)), RuntimeError(PRIVATE_BODY)]
    result = runtime.start()
    assert result.run.status is RunStatus.FAILED
    assert result.answer is None
    assert runtime.store.load_run(CONTEXT).conversation.items[-1] == VisibleResult(
        PUBLIC_CALL.call_id, {"total": 42}
    )


@pytest.mark.parametrize("stage", [WaitReason.APPROVAL, WaitReason.JOB])
def test_cancelled_wait_blocks_late_events_and_further_model_calls(
    runtime: RuntimeHarness, stage: WaitReason
) -> None:
    runtime.model.replies = [ModelReply(calls=(REPORT_CALL,))]
    waiting = runtime.start()
    event = runtime.events.decide(CONTEXT, waiting.run.pending_calls[0], granted=True)

    if stage is WaitReason.JOB:
        waiting = runtime.continuations.resume(CONTEXT, event)
        event = event_for(waiting.run.pending_calls[0])

    result = runtime.continuations.resume(CONTEXT, event, is_cancelled=lambda: True)
    assert result.run.status is RunStatus.CANCELLED
    assert result.artifacts == ()
    assert len(runtime.model.requests) == 1

    with pytest.raises(StateConflictError):
        runtime.continuations.resume(CONTEXT, event)

    assert len(runtime.executions) == (1 if stage is WaitReason.JOB else 0)


def test_mixed_pending_approvals_and_jobs_complete_out_of_order(runtime: RuntimeHarness) -> None:
    second = ToolCall("report-2", REPORT.name, {"dataset": "sample", "total": 7})
    runtime.model.replies = [
        ModelReply(calls=(REPORT_CALL, second)),
        ModelReply(answer="Both reports are ready."),
    ]
    waiting = runtime.start()
    first_approval, second_approval = waiting.run.pending_calls
    event = runtime.events.decide(CONTEXT, second_approval, granted=True)
    waiting = runtime.continuations.resume(CONTEXT, event)
    second_job = next(call for call in waiting.run.pending_calls if call.reason is WaitReason.JOB)
    partial = runtime.continuations.resume(CONTEXT, event_for(second_job))
    assert partial.run.status is RunStatus.WAITING
    assert partial.run.pending_calls == (first_approval,)
    assert partial.artifacts[0].call_id == second.call_id
    assert len(runtime.model.requests) == 1

    first_event = runtime.events.decide(CONTEXT, first_approval, granted=True)
    waiting = runtime.continuations.resume(CONTEXT, first_event)
    final = runtime.continuations.resume(CONTEXT, event_for(waiting.run.pending_calls[0]))
    assert final.run.status is RunStatus.COMPLETED
    assert final.artifacts[0].call_id == REPORT_CALL.call_id
    assert len(runtime.executions) == 2
    assert len(runtime.model.requests) == 2


def test_interleaved_sessions_keep_job_results_and_reports_owned(runtime: RuntimeHarness) -> None:
    other = replace(CONTEXT, user_id="other-user", conversation_id="other-chat", run_id="other-run")
    runtime.access.permissions[(other.tenant_id, other.user_id)] = PERMISSIONS
    runtime.access.datasets[(other.tenant_id, other.user_id)] = frozenset({"sample"})
    runtime.model.replies = [
        ModelReply(calls=(REPORT_CALL,)),
        ModelReply(calls=(REPORT_CALL,)),
        ModelReply(answer="First report ready."),
        ModelReply(answer="Second report ready."),
    ]
    first = runtime.start(CONTEXT, "First user's request")
    second = runtime.start(other, "Second user's request")

    for context, waiting in ((CONTEXT, first), (other, second)):
        event = runtime.events.decide(context, waiting.run.pending_calls[0], granted=True)
        waiting = runtime.continuations.resume(context, event)
        result = runtime.continuations.resume(context, event_for(waiting.run.pending_calls[0]))
        assert result.run.status is RunStatus.COMPLETED
        assert PRIVATE_BODY in runtime.read_report(context, result.artifacts[0].artifact_id)

    assert "Second user's request" not in runtime.model.request_payloads[2]
    assert "First user's request" not in runtime.model.request_payloads[3]
    assert runtime.executions == [(REPORT.name, CONTEXT), (REPORT.name, other)]


def test_leakage_snapshot_includes_fields_hidden_from_repr() -> None:
    request = ModelRequest((VisibleResult("call", {"private": PRIVATE_BODY}),), ())
    assert PRIVATE_BODY not in repr(request)
    assert PRIVATE_BODY in inspect_fields(request)
