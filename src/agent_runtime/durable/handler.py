"""Resume waiting runs through mocked job and approval providers."""

from collections.abc import Callable

from ..conversations.conversation import ConversationItem
from ..coordinator.control import CoordinatorResult
from ..coordinator.runner import Coordinator
from ..policy.output import EnforcedOutput
from ..runs.context import RunContext
from ..runs.state import RunState, RunStatus
from ..storage.interface import Checkpoint, StateConflictError, StateStore
from ..tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    ToolFailure,
    VisibleResult,
)
from .continuation import (
    ContinuationEvent,
    ContinuationResolver,
    match_pending_call,
    resolve_pending_call,
)


class ContinuationHandler:
    """Claim and resolve waiting work, then hand its checkpoint to the coordinator."""

    def __init__(
        self, store: StateStore, coordinator: Coordinator, resolver: ContinuationResolver
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._resolver = resolver

    def resume(
        self,
        context: RunContext,
        event: ContinuationEvent,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoordinatorResult:
        """Resolve one matching wait and continue once all outstanding calls have results."""

        # Example: a waiting run has call-2/job-17 and call-3/job-18. A
        # notification for job-17 must first match its saved call/reason/reference
        # under the authenticated context. The notification carries no result or
        # permission; the resolver obtains the policy-enforced outcome separately.
        checkpoint = self._store.load_run(context)
        pending = match_pending_call(checkpoint.run, event)

        # Save WAITING -> RUNNING before contacting the resolver, which may
        # execute an approved action. Revision checks reject conflicting updates;
        # PostgreSQL can return the saved outcome for identical retries. Synthetic
        # callers must serialize execution until durable worker ownership exists.
        checkpoint = self._save(checkpoint, RunStatus.RUNNING, pending=checkpoint.run.pending_calls)
        artifacts: list[ArtifactReference] = []

        if is_cancelled is not None and is_cancelled():
            return self._cancel(checkpoint, artifacts)

        output = resolve_pending_call(self._resolver, context, pending)
        remaining = tuple(
            call for call in checkpoint.run.pending_calls if call.call_id != pending.call_id
        )

        # Approving a report may submit a job. Replace approval-3 with job-17;
        # a second approval-3 notification will no longer match pending work.
        if isinstance(output.model_result, PendingToolCall):
            remaining += (output.model_result,)

        if output.artifact is not None:
            artifacts.append(output.artifact)

        # Save job-17's receipt, its opaque delivery metadata and the remaining
        # job-18 wait together. If storage rejects the transition, no partial
        # receipt is accepted as the next model context. Earlier external effects
        # still require their own reconciliation after a crash.
        status = RunStatus.WAITING if remaining else RunStatus.RUNNING
        checkpoint = self._save(
            checkpoint, status, (output.model_result,), remaining, artifacts=tuple(artifacts)
        )

        if is_cancelled is not None and is_cancelled():
            return self._cancel(checkpoint, artifacts)

        # With two jobs, the first completion saves progress; the second makes
        # both results available to the next model call.
        if remaining:
            return CoordinatorResult(
                checkpoint.run,
                artifacts=tuple(artifacts),
                conversation_revision=checkpoint.conversation.revision,
            )

        return self._coordinator.continue_run(
            checkpoint, artifacts=tuple(artifacts), is_cancelled=is_cancelled
        )

    @staticmethod
    def child_outcome(
        run: RunState,
        call_id: str,
        items: tuple[ConversationItem, ...],
        artifacts: tuple[ArtifactReference, ...],
    ) -> EnforcedOutput:
        """Validate an already enforced child result before the durable group joins.

        A worker settling call-b cannot satisfy call-a's wait. The storage adapter
        serializes this check with child settlement and schedules the same next
        coordinator boundary once every ordered outcome exists.
        """

        if run.status != RunStatus.WAITING or call_id not in {
            pending.call_id for pending in run.pending_calls
        }:
            raise StateConflictError("child does not belong to a waiting parent")

        if len(items) != 1 or not isinstance(
            items[0], (VisibleResult, RestrictedReceipt, ToolFailure)
        ):
            raise ValueError("child requires one enforced terminal outcome")

        if items[0].call_id != call_id or len(artifacts) > 1:
            raise StateConflictError("child outcome identity differs")

        if artifacts and artifacts[0].call_id != call_id:
            raise StateConflictError("child delivery identity differs")

        return EnforcedOutput(items[0], artifacts[0] if artifacts else None)

    def _save(
        self,
        checkpoint: Checkpoint,
        status: RunStatus,
        items: tuple[ConversationItem, ...] = (),
        pending: tuple[PendingToolCall, ...] = (),
        *,
        artifacts: tuple[ArtifactReference, ...] = (),
    ) -> Checkpoint:
        return self._store.save_checkpoint(
            checkpoint.run.context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            status=status,
            items=items,
            pending_calls=pending,
            artifacts=artifacts,
            expected_recovery_id=checkpoint.conversation.recovery_id,
            expected_deletion_generation=checkpoint.conversation.deletion_generation,
        )

    def _cancel(
        self, checkpoint: Checkpoint, artifacts: list[ArtifactReference]
    ) -> CoordinatorResult:
        checkpoint = self._save(checkpoint, RunStatus.CANCELLED)
        return CoordinatorResult(
            checkpoint.run,
            artifacts=tuple(artifacts),
            conversation_revision=checkpoint.conversation.revision,
        )
