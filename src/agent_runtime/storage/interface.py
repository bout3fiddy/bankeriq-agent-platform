"""Storage operations and consistency requirements for conversation/run state."""

from dataclasses import dataclass
from typing import Protocol

from ..conversations.conversation import Conversation, ConversationItem
from ..runs.context import RunContext
from ..runs.state import RunState, RunStatus
from ..tools.results import ArtifactReference, PendingToolCall


class StateNotFoundError(LookupError):
    """The requested state is unavailable to this tenant/user/context."""


class StateConflictError(RuntimeError):
    """An identifier, revision or active-run ownership conflicts with stored state."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Run-specific conversation context and progress read or written together."""

    conversation: Conversation
    run: RunState


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """Bounded journal items with a resume cursor, including metadata-only changes."""

    items: tuple[ConversationItem, ...]
    next_cursor: int
    durable_cursor: int
    deletion_generation: int
    recovery_id: str


class StateStore(Protocol):
    """Trusted callers supply verified identity and policy-approved history items.

    Execution writes are atomic and reject stale revisions. Durable admission
    may queue later turns without changing the active execution revision.
    """

    def create_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        """Create an empty owned conversation; duplicate tenant-scoped IDs conflict."""

        ...

    def load_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        """Return current history after checking ownership."""

        ...

    def create_run(
        self,
        context: RunContext,
        *,
        expected_conversation_revision: int,
        items: tuple[ConversationItem, ...] = (),
    ) -> Checkpoint:
        """Admit input and a queued run; durable stores may retain an earlier active run."""

        ...

    def load_run(self, context: RunContext) -> Checkpoint:
        """Read run state and its permitted history under the supplied ownership."""

        ...

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
    ) -> Checkpoint:
        """Atomically save progress; omitted budget counters retain their stored values."""

        ...

    def deliveries(self, context: RunContext) -> tuple[ArtifactReference, ...]:
        """Return permitted opaque delivery metadata after checking ownership."""

        ...
