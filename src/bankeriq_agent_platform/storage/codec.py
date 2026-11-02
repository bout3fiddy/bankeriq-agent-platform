"""Version-one journal encoding with explicit permitted variants and size bounds."""

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from hashlib import sha256
from typing import Any

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.conversations.history import snapshot_items
from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    PendingToolCall,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
    WaitReason,
)


def plain(value: Any) -> Any:
    """Convert immutable runtime values to JSON without serialising arbitrary objects."""

    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}

    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}

    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]

    return value


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def encode_items(items: tuple[ConversationItem, ...]) -> list[dict[str, Any]]:
    encoded = [
        {"type": type(item).__name__, "value": plain(item)} for item in snapshot_items(items)
    ]

    if len(encoded) > 100 or len(json.dumps(encoded).encode()) > 131072:
        raise ValueError("checkpoint delta exceeds storage bound")

    if any(len(json.dumps(item).encode()) > 60000 for item in encoded):
        raise ValueError("journal event exceeds storage bound")

    return encoded


def decode_item(record: dict[str, Any]) -> ConversationItem:
    value = record["value"]

    match record["type"]:
        case "Message":
            return Message(MessageRole(value["role"]), value["text"])
        case "ToolCall":
            return ToolCall(value["call_id"], value["tool_name"], value["arguments"])
        case "VisibleResult":
            return VisibleResult(value["call_id"], value["payload"])
        case "RestrictedReceipt":
            return RestrictedReceipt(value["call_id"])
        case "ToolFailure":
            return ToolFailure(value["call_id"], ToolErrorCode(value["code"]))
        case "PendingToolCall":
            return PendingToolCall(
                value["call_id"], WaitReason(value["reason"]), value["reference_id"]
            )
        case _:
            raise ValueError("unsupported journal item version")
