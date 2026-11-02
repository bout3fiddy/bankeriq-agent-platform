"""Context selection retains complete turns and fails when required input cannot fit."""

import pytest

from agent_runtime.conversations.context import ContextBudgetExceeded, bounded_context
from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import VisibleResult


def test_context_drops_complete_old_turns_and_preserves_pairs() -> None:
    first = (
        Message(MessageRole.USER, "first"),
        ToolCall("one", "sum", {}),
        VisibleResult("one", {"total": 42}),
        Message(MessageRole.ASSISTANT, "42"),
    )
    second = (Message(MessageRole.USER, "second"), ToolCall("two", "sum", {}))
    assert bounded_context(first + second, instructions="policy", tools=(), max_items=3) == second

    with pytest.raises(ContextBudgetExceeded):
        bounded_context(first + second, instructions="policy", tools=(), max_items=1)


def test_instructions_and_tools_consume_the_context_budget() -> None:
    items = (Message(MessageRole.USER, "hello"),)

    with pytest.raises(ContextBudgetExceeded):
        bounded_context(items, instructions="x" * 100, tools=(), max_bytes=100)

    with pytest.raises(ContextBudgetExceeded):
        bounded_context(items, instructions="policy", tools=({"schema": "x" * 100},), max_bytes=100)
