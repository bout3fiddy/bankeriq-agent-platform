"""Public runtime types shared by the coordinator and tool boundary."""

from .enums import MessageRole, OutputPolicy, RunStatus, ToolErrorCode, WaitReason
from .json_value import JsonValue
from .messages import Message
from .results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    ToolFailure,
    ToolResult,
    VisibleResult,
)
from .runs import RunContext, RunState
from .tools import ToolCall, ToolDefinition

type ConversationItem = Message | ToolCall | ToolResult

__all__ = [
    "ArtifactReference",
    "ConversationItem",
    "JsonValue",
    "Message",
    "MessageRole",
    "OutputPolicy",
    "PendingToolCall",
    "RestrictedReceipt",
    "RunContext",
    "RunState",
    "RunStatus",
    "ToolCall",
    "ToolDefinition",
    "ToolErrorCode",
    "ToolFailure",
    "ToolResult",
    "VisibleResult",
    "WaitReason",
]
