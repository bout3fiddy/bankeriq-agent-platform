"""Checks for values admitted to stored coordinator history."""

from agent_harness.conversations.conversation import ConversationItem
from agent_harness.conversations.messages import Message
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import (
    PendingToolCall,
    RestrictedReceipt,
    ToolFailure,
    VisibleResult,
)


def snapshot_items(items: tuple[ConversationItem, ...]) -> tuple[ConversationItem, ...]:
    """Capture allowed history records at the storage boundary."""

    # Passing EnforcedOutput.artifact or a raw ExecutionOutput here is a routing
    # error. Only the already-enforced model_result belongs in conversation history.
    allowed = (Message, ToolCall, VisibleResult, RestrictedReceipt, ToolFailure, PendingToolCall)
    snapshot = tuple(items)

    if any(type(item) not in allowed for item in snapshot):
        raise ValueError("unsupported conversation item")

    return snapshot
