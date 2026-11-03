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
    """Unwrap runtime structures; the JSON encoder rejects unsupported terminal values."""

    # ToolCall("call-1", "sum", {"values": (2, 3)}) contains frozen mappings
    # and tuples. Unwrap those to {"call_id": "call-1", "tool_name": "sum",
    # "arguments": {"values": [2, 3]}} before passing the result to Jsonb.
    # This helper also handles checkpoint dataclasses; encode_items separately
    # limits which dataclass types may enter model-visible conversation history.
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}

    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}

    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]

    return value


def strict_json(value: Any, *, maximum: int = 262144) -> str:
    """Reject coercions and unsupported JSON values before a transaction starts."""

    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError("JSON object keys must be strings")

            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError("unsupported JSON value")

    validate(value)
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)

    if len(encoded.encode()) > maximum:
        raise ValueError("JSON storage bound exceeded")

    return encoded


def fingerprint(value: Any) -> str:

    # {"a": 1, "b": 2} and {"b": 2, "a": 1} represent the same JSON object.
    # Sorting keys gives both the same digest, while changing b to 3 conflicts
    # when a caller tries to reuse the same admission or transition identity.
    return sha256(strict_json(plain(value)).encode()).hexdigest()


def encode_items(items: tuple[ConversationItem, ...]) -> list[dict[str, Any]]:

    # For example, RestrictedReceipt("call-1") becomes a tagged record with
    # only its call ID. snapshot_items rejects raw ExecutionOutput and delivery
    # references before this serializer can copy their fields into the journal.
    encoded = [
        {"type": type(item).__name__, "value": plain(item)} for item in snapshot_items(items)
    ]

    # Bound the entire write and each member independently. A 101-item delta
    # and a single 60,001-byte event both fail before storage starts the write;
    # the individual limit also leaves headroom below the database row limit.
    if len(encoded) > 100 or len(strict_json(encoded).encode()) > 131072:
        raise ValueError("checkpoint delta exceeds storage bound")

    if any(len(strict_json(item).encode()) > 60000 for item in encoded):
        raise ValueError("journal event exceeds storage bound")

    return encoded


def decode_item(record: dict[str, Any]) -> ConversationItem:

    # The saved tag selects an explicit runtime variant. For example, decoding
    # a RestrictedReceipt reconstructs its call ID without adding a payload or
    # artifact field. An unknown tag requires an explicit format upgrade.
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
