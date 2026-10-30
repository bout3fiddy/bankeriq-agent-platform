"""Checks for values admitted to stored coordinator history."""

from ..tools.definitions import ToolCall
from ..tools.results import PendingToolCall, RestrictedReceipt, ToolFailure, VisibleResult
from .conversation import ConversationItem
from .messages import Message


def snapshot_items(items: tuple[ConversationItem, ...]) -> tuple[ConversationItem, ...]:
    """Capture allowed history records at the storage boundary."""

    # Passing EnforcedOutput.artifact or a raw ExecutionOutput here is a routing
    # error. Only the already-enforced model_result belongs in conversation history.
    allowed = (Message, ToolCall, VisibleResult, RestrictedReceipt, ToolFailure, PendingToolCall)
    snapshot = tuple(items)

    if any(type(item) not in allowed for item in snapshot):
        raise ValueError("unsupported conversation item")

    return snapshot
