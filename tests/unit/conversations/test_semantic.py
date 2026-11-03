"""Source fidelity and full-request token boundaries, without services or model traffic."""

import json
from dataclasses import replace

import pytest

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.model import ModelRequest, ModelTool
from agent_harness.memory.semantic import (
    MemoryBlocked,
    MemoryFact,
    TokenBudget,
    memory_message,
    select_facts,
)
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter


def test_exact_provenance_and_execution_truth() -> None:
    facts = (
        MemoryFact(7, 1, "user", "Payment completed according to me."),
        MemoryFact(9, 1, "tool_failed", "Tool failed."),
        MemoryFact(11, 2, "user", "Use EUR", "decision"),
        MemoryFact(12, 2, "user", "Which date?", "open_question"),
    )
    selected = select_facts(
        json.dumps(
            {
                "entries": [
                    {"source": 7, "quote": "Payment completed", "label": "fact"},
                ]
            }
        ),
        facts,
    )
    assert selected[0].kind == "user"
    assert any(fact.source == 9 and fact.kind == "tool_failed" for fact in selected)
    assert all(fact in selected for fact in facts[2:])
    assert memory_message(selected).role == MessageRole.USER


@pytest.mark.parametrize(
    "entry",
    [
        {"source": 99, "quote": "Use EUR", "label": "decision"},
        {"source": 7, "quote": "payment completed", "label": "fact"},
        {"source": 7, "quote": "Use EUR", "label": "approved"},
        {"source": True, "quote": "Use EUR", "label": "fact"},
        {"source": 7, "quote": "Use EUR", "label": "fact", "kind": "tool_completed"},
    ],
)
def test_reject_invented_or_relabelled_sources(entry: dict) -> None:
    with pytest.raises(MemoryBlocked):
        select_facts(json.dumps({"entries": [entry]}), (MemoryFact(7, 1, "user", "Use EUR"),))


def test_budget_counts_instructions_tools_unicode_and_output() -> None:
    counter = FoundryTokenCounter("gpt-4o")
    request = ModelRequest((Message(MessageRole.USER, "日本語 <|endoftext|> €42"),), ())
    count = counter(request)
    budget = TokenBudget("gpt-4o", count + 256, 256, counter)
    budget.require(request)

    with pytest.raises(MemoryBlocked):
        replace(budget, window=count + 255).require(request)

    for enlarged in (
        replace(request, instructions="instruction " * 100),
        replace(request, tools=(ModelTool("lookup", "schema " * 100, {"type": "object"}),)),
    ):
        with pytest.raises(MemoryBlocked):
            budget.require(enlarged)


def test_output_schema_is_budgeted_and_immutable() -> None:
    counter = FoundryTokenCounter("gpt-4o")
    base = ModelRequest((), (), instructions="Return JSON.")
    schema = {"type": "object", "description": "schema detail " * 100}
    request = replace(base, output_schema=schema)
    counted = counter(request)
    schema["description"] = "changed after budgeting"
    assert counter(request) == counted
    assert counted > counter(base)

    with pytest.raises(MemoryBlocked):
        TokenBudget("structured", counter(base) + 256, 256, counter).require(request)


def test_candidate_selection_keeps_text_and_execution_origin_exact() -> None:
    from agent_harness.memory.semantic import select_candidates, summary_candidates

    sources = (
        MemoryFact(7, 1, "user", "**Decision:** Use EUR. The payment is complete according to me."),
        MemoryFact(9, 1, "tool_failed", "Tool failed."),
    )
    candidates = summary_candidates(sources)
    selected = select_candidates(
        '{"entries":[{"candidate":0,"label":"decision"},{"candidate":1,"label":"fact"}]}',
        candidates,
        sources,
    )
    assert selected[0].text == "**Decision:** Use EUR."
    assert selected[0].source == 7 and selected[0].kind == "user"
    assert selected[1].kind == "user"
    assert selected[2].kind == "tool_failed"

    for entry in (
        {"candidate": 99, "label": "fact"},
        {"candidate": True, "label": "fact"},
        {"candidate": 0, "label": "approved"},
        {"candidate": 0, "label": "fact", "quote": "invented text"},
    ):
        with pytest.raises(MemoryBlocked):
            select_candidates(json.dumps({"entries": [entry]}), candidates, sources)


def test_candidate_text_and_count_bounds() -> None:
    from agent_harness.memory.semantic import summary_candidates

    source = MemoryFact(1, 1, "user", "Long text " * 500)
    candidates = summary_candidates((source,))
    assert all(len(f.text) <= 1024 and f.text in source.text for f in candidates)

    with pytest.raises(MemoryBlocked, match="summary_candidate_limit"):
        summary_candidates((MemoryFact(1, 1, "user", "x" * (257 * 1024)),))
