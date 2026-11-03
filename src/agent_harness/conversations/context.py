"""Bound model input by complete turns; semantic compaction is intentionally absent."""

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from agent_harness.conversations.conversation import ConversationItem
from agent_harness.conversations.messages import Message, MessageRole


class ContextBudgetExceeded(ValueError):
    """The newest turn and required instructions/tools cannot fit the configured input budget."""


def bounded_context(
    items: tuple[ConversationItem, ...],
    *,
    instructions: str,
    tools: tuple,
    max_items: int = 200,
    max_bytes: int = 262144,
) -> tuple[ConversationItem, ...]:
    """Keep the newest complete user turns with instructions and tool schemas reserved.

    A user message starts a turn. Removing an older turn removes all of its tool
    calls and results together. For example, A(call,result), B(user) becomes B
    when A/B exceed the budget; B(call without result) stays intact while waiting.
    This is an encoded UTF-8 size bound, not a provider token-count guarantee.
    """

    starts = [
        index
        for index, item in enumerate(items)
        if isinstance(item, Message) and item.role is MessageRole.USER
    ]
    starts = sorted({0, *starts})

    for start in starts:
        candidate = items[start:]
        size = len(
            json.dumps(
                [instructions, tools, candidate], default=_plain, ensure_ascii=True, allow_nan=False
            ).encode("utf-8")
        )

        if len(candidate) <= max_items and size <= max_bytes:
            return candidate

    raise ContextBudgetExceeded("current turn exceeds model context budget; compaction required")


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}

    if isinstance(value, Mapping):
        return dict(value)

    raise TypeError("unsupported model context value")
