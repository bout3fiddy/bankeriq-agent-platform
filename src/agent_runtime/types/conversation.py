"""Items admitted to coordinator conversation history."""

from ..tools.definitions import ToolCall
from ..tools.results import ToolResult
from .messages import Message

type ConversationItem = Message | ToolCall | ToolResult
