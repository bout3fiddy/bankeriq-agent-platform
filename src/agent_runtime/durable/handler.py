"""Resume waiting runs through mocked job and approval providers."""

from collections.abc import Callable

from ..conversations.conversation import ConversationItem
from ..coordinator.control import CoordinatorResult
from ..coordinator.runner import Coordinator
from ..runs.context import RunContext
from ..runs.state import RunStatus
from ..storage.interface import Checkpoint, StateStore
from ..tools.results import ArtifactReference, PendingToolCall
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

        checkpoint = self._store.load_run(context)
        pending = match_pending_call(checkpoint.run, event)

        # Claim before contacting the resolver, which may execute an approved
        # action. Two consumers of job-17 cannot both save this revision.
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
