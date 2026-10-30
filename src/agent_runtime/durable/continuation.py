"""Durable-layer event matching and trusted job/approval resolution interfaces."""

from dataclasses import dataclass
from typing import Protocol

from ..policy.output import EnforcedOutput, enforce_failure
from ..runs.context import RunContext
from ..runs.state import RunState, RunStatus
from ..storage.interface import StateConflictError
from ..tools.results import PendingToolCall, WaitReason


@dataclass(frozen=True, slots=True)
class ContinuationEvent:
    """Notification identifying a pending call; the transport supplies verified context."""

    # Example: job-17 completed for call-2. The notification carries only IDs;
    # the resolver retrieves the outcome from the trusted job/approval provider.
    call_id: str
    reason: WaitReason
    reference_id: str


class ContinuationResolver(Protocol):
    """Trusted job/approval integration, supplied by a local fake in this phase."""

    # For an approval, look up the original call and pinned version using the
    # reference, then dispatch through the gateway. Its access provider must
    # consume an approval bound to that exact context, version and arguments.
    # For a job, recheck access and apply output policy to the completed result.
    # An incoming event alone grants no permission or approval.
    def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
        """Return an enforced outcome, or an approval-to-job handoff receipt."""

        ...


def match_pending_call(run: RunState, event: ContinuationEvent) -> PendingToolCall:
    """Reject duplicates and unrelated events before claiming or executing work."""

    if run.status is not RunStatus.WAITING:
        raise StateConflictError("continuation requires a waiting run")

    for pending in run.pending_calls:
        if (
            pending.call_id == event.call_id
            and pending.reason == event.reason
            and pending.reference_id == event.reference_id
        ):
            return pending

    # Once call-2/job-17 is resolved, it leaves pending_calls. A duplicate event
    # then fails this lookup even if other jobs are still keeping the run waiting.
    raise StateConflictError("event does not match pending work")


def resolve_pending_call(
    resolver: ContinuationResolver, context: RunContext, pending: PendingToolCall
) -> EnforcedOutput:
    """Keep adapter failures and mismatched results out of model history."""

    try:
        output = resolver.resolve(context, pending)
    except Exception:
        return enforce_failure(pending.call_id)

    # A result for call-9 must never satisfy the notification for call-2.
    if output.model_result.call_id != pending.call_id:
        return enforce_failure(pending.call_id)

    if output.artifact is not None and output.artifact.call_id != pending.call_id:
        return enforce_failure(pending.call_id)

    if isinstance(output.model_result, PendingToolCall):

        # Approval may start a queued job: approval-3 -> job-17. A completion
        # event must otherwise resolve its wait, so the same event cannot repeat it.
        if pending.reason is not WaitReason.APPROVAL:
            return enforce_failure(pending.call_id)

        if output.model_result.reason is not WaitReason.JOB:
            return enforce_failure(pending.call_id)

    return output
