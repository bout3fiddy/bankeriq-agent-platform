"""Storage operations and consistency requirements for conversation/run state."""

from dataclasses import dataclass
from typing import Protocol

from ..conversations.conversation import Conversation, ConversationItem
from ..runs.context import RunContext
from ..runs.state import RunState, RunStatus
from ..tools.results import PendingToolCall


class StateNotFoundError(LookupError):
    """The requested state is unavailable to this tenant/user/context."""


class StateConflictError(RuntimeError):
    """An identifier, revision or active-run ownership conflicts with stored state."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Conversation and run snapshots read or written together."""

    conversation: Conversation
    run: RunState


class StateStore(Protocol):
    """Trusted callers supply verified identity and policy-approved history items.

    Writes must be atomic and reject stale revisions. A database adapter will
    implement these guarantees with transactions and concurrency controls.
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
        """Atomically append input and create a queued run as the conversation's active run."""

        ...

    def load_run(self, context: RunContext) -> Checkpoint:
        """Read run state and current conversation history under the supplied ownership."""

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
    ) -> Checkpoint:
        """Atomically save progress; omitted budget counters retain their stored values."""

        ...
