"""Items admitted to coordinator conversation history."""

from dataclasses import dataclass, field

from agent_harness.conversations.messages import Message
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import ToolResult

type ConversationItem = Message | ToolCall | ToolResult


@dataclass(frozen=True, slots=True)
class Conversation:
    """Owned, ordered history and the run currently allowed to update it."""

    tenant_id: str
    user_id: str
    conversation_id: str
    items: tuple[ConversationItem, ...] = field(default=(), repr=False)
    revision: int = 0
    active_run_id: str | None = None
    deletion_generation: int = 0
    recovery_id: str | None = None

    # Durable settlement can exceed the model window. Keep its results committed
    # and mark the derived view incomplete; callers must page history or compact
    # before another model step. Never infer from an empty/truncated current turn.
    context_complete: bool = True
