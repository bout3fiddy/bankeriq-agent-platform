"""Items admitted to coordinator conversation history."""

from dataclasses import dataclass, field

from ..tools.definitions import ToolCall
from ..tools.results import ToolResult
from .messages import Message

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
