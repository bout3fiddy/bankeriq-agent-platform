"""Thread-safe local state store; contents live for this instance's lifetime."""

from dataclasses import replace
from threading import RLock

from ..conversations.conversation import Conversation, ConversationItem
from ..conversations.history import snapshot_items
from ..prompts.resolution import PromptResolver
from ..runs.context import RunContext
from ..runs.state import TERMINAL_STATUSES, RunState, RunStatus, validate_transition
from ..tools.results import ArtifactReference, PendingToolCall
from ._locking import synchronized
from .interface import Checkpoint, StateConflictError, StateNotFoundError


class InMemoryStateStore:
    """Exercise ownership and atomic checkpoint semantics with immutable snapshots."""

    def __init__(self) -> None:
        self._conversations: dict[tuple[str, str], Conversation] = {}
        self._runs: dict[tuple[str, str], RunState] = {}
        self._artifacts: dict[tuple[str, str], tuple[ArtifactReference, ...]] = {}
        self._prepared: dict[tuple[str, str, int], Checkpoint] = {}
        self._lock = RLock()

    @synchronized
    def create_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        key = (tenant_id, conversation_id)

        if key in self._conversations:
            raise StateConflictError("conversation already exists")

        conversation = Conversation(tenant_id, user_id, conversation_id)
        self._conversations[key] = conversation
        return conversation

    @synchronized
    def load_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        conversation = self._conversations.get((tenant_id, conversation_id))

        if conversation is None or conversation.user_id != user_id:
            raise StateNotFoundError("conversation unavailable")

        return conversation

    @synchronized
    def create_run(
        self,
        context: RunContext,
        *,
        expected_conversation_revision: int,
        items: tuple[ConversationItem, ...] = (),
    ) -> Checkpoint:
        """Append input and reserve the conversation for a new queued run."""

        # Example: the caller loaded conversation revision 3 and received a new
        # user message. It supplies a fresh run_id in context, then calls:
        #
        # store.create_run(context, expected_conversation_revision=3,
        #                  items=(Message(MessageRole.USER, "Calculate a total"),))
        #
        # The result contains conversation revision 4 and a queued run at revision
        # 0. The standalone * requires named arguments for the revision and items,
        # making the meaning of those values explicit at each call site.

        # Verify that the conversation belongs to the context's tenant and user
        # before attaching work to it. The caller supplies verified identity.
        conversation = self.load_conversation(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )

        # If another request advanced revision 3 to 4, this caller's view is stale.
        # Rejecting the write lets it reload history before deciding what to do.
        if conversation.revision != expected_conversation_revision:
            raise StateConflictError("conversation revision changed")

        # Reserve one active run per conversation so overlapping requests cannot
        # generate competing answers from the same history. A waiting run keeps
        # this reservation until it finishes, fails or is cancelled.
        if conversation.active_run_id is not None:
            raise StateConflictError("conversation already has an active run")

        # Tenant-scoped keys allow separate organisations to reuse a run ID.
        run_key = (context.tenant_id, context.run_id)

        # Reusing an ID could overwrite earlier progress, including a finished
        # run. Reject it so the caller can inspect the existing run explicitly.
        if run_key in self._runs:
            raise StateConflictError("run already exists")

        # QUEUED records accepted work. The coordinator advances it to RUNNING
        # when execution begins. Build both snapshots before changing storage so
        # an invalid history item leaves the existing conversation untouched.
        run = RunState(context, RunStatus.QUEUED)
        conversation = replace(
            conversation,
            items=conversation.items + snapshot_items(items),
            revision=conversation.revision + 1,
            active_run_id=context.run_id,
        )

        # The decorator holds the lock across all checks and both writes. Readers
        # therefore see the appended input, reservation and run record together.
        self._conversations[(context.tenant_id, context.conversation_id)] = conversation
        self._runs[run_key] = run
        return Checkpoint(conversation, run)

    @synchronized
    def load_run(self, context: RunContext) -> Checkpoint:
        conversation = self.load_conversation(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )
        run = self._runs.get((context.tenant_id, context.run_id))

        if run is None or run.context != context:
            raise StateNotFoundError("run unavailable")

        return Checkpoint(conversation, run)

    @synchronized
    def save_checkpoint(
        self,
        context: RunContext,
        *,
        expected_conversation_revision: int,
        expected_run_revision: int,
        status: RunStatus,
        items: tuple[ConversationItem, ...] = (),
        pending_calls: tuple[PendingToolCall, ...] = (),
        model_steps: int | None = None,
        tool_calls: int | None = None,
        artifacts: tuple[ArtifactReference, ...] = (),
        expected_recovery_id: str | None = None,
        expected_deletion_generation: int = 0,
        prompt_resolver: PromptResolver | None = None,
    ) -> Checkpoint:

        # Example: append a restricted completion receipt and change WAITING to
        # RUNNING in one write. Both revisions must still match the loaded values.
        # The lock covers validation and both replacements, so competing threads
        # cannot save an answer against stale history or observe a partial update.
        current = self.load_run(context)
        prior = self._prepared.get((context.tenant_id, context.run_id, expected_run_revision))

        if prompt_resolver is not None and prior is not None:
            if (
                prior.conversation.revision != expected_conversation_revision + 1
                or model_steps != prior.run.model_steps
                or status is not RunStatus.RUNNING
                or items
                or pending_calls
                or artifacts
                or tool_calls is not None
            ):
                raise StateConflictError("prepared step conflicts")

            assert prior.run.prompt_manifest is not None
            prompt_resolver.instructions(prior.run.prompt_manifest)
            return prior

        if (
            current.conversation.revision != expected_conversation_revision
            or current.run.revision != expected_run_revision
            or current.conversation.active_run_id != context.run_id
        ):
            raise StateConflictError("checkpoint changed or run is inactive")

        if prompt_resolver is not None and current.run.pending_calls:
            raise StateConflictError("model preparation requires resolved tool calls")

        if prompt_resolver is not None and (
            status is not RunStatus.RUNNING
            or items
            or pending_calls
            or artifacts
            or model_steps != current.run.model_steps + 1
            or tool_calls is not None
        ):
            raise ValueError("prompt preparation must reserve exactly one model step")

        validate_transition(current.run.status, status)

        # Waiting and continuation writes retain usage. For example, a run that
        # paused after two model calls resumes with those two calls still counted.
        run = replace(
            current.run,
            status=status,
            pending_calls=tuple(pending_calls),
            revision=current.run.revision + 1,
            model_steps=current.run.model_steps if model_steps is None else model_steps,
            tool_calls=current.run.tool_calls if tool_calls is None else tool_calls,
            prompt_manifest=prompt_resolver.resolve()
            if prompt_resolver is not None
            else current.run.prompt_manifest,
        )
        conversation = replace(
            current.conversation,
            items=current.conversation.items + snapshot_items(items),
            revision=current.conversation.revision + 1,
            active_run_id=None if status in TERMINAL_STATUSES else context.run_id,
        )

        # Construct and validate both records before mutating either index.
        self._conversations[(context.tenant_id, context.conversation_id)] = conversation
        self._runs[(context.tenant_id, context.run_id)] = run
        key = (context.tenant_id, context.run_id)
        self._artifacts[key] = self._artifacts.get(key, ()) + artifacts
        result = Checkpoint(conversation, run)

        if prompt_resolver is not None:
            self._prepared[(context.tenant_id, context.run_id, expected_run_revision)] = result

        return result

    @synchronized
    def load_model_step(self, context: RunContext, step_number: int) -> Checkpoint:
        self.load_run(context)

        for (tenant, run, _), checkpoint in self._prepared.items():
            if (
                tenant == context.tenant_id
                and run == context.run_id
                and checkpoint.run.model_steps == step_number
            ):
                return checkpoint

        raise StateNotFoundError("prepared model step unavailable")

    @synchronized
    def deliveries(self, context: RunContext) -> tuple[ArtifactReference, ...]:
        self.load_run(context)
        return self._artifacts.get((context.tenant_id, context.run_id), ())
