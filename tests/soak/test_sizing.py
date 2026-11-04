"""Keep the reviewed endurance fixture large enough without growing protected facts."""

import json
from pathlib import Path

import pytest

from agent_harness.memory.semantic import MemoryFact, summary_candidates
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from tests.soak.sizing import estimate


def test_disposable_example_is_bounded_and_has_no_protected_facts() -> None:
    text = " ".join(json.loads(Path("tests/soak/dialogue.json").read_text())[0])
    counter = FoundryTokenCounter("gpt-4o-2024-11-20")
    candidates = summary_candidates((MemoryFact(1, 1, "user", text),))

    assert len(text.encode()) <= 8192
    assert 1000 <= len(counter.encoding.encode(text, disallowed_special=())) <= 1500
    assert candidates
    assert all(fact.label == "fact" for fact in candidates)


@pytest.mark.parametrize("extra_summary_facts", [0, 4])
def test_compaction_pressure_survives_fewer_turns(extra_summary_facts: int) -> None:

    # Thirty-two turns leaves room for realistic latency in the two-hour schedule.
    # The real gate still reads five published versions from each hosted canary;
    # a pure policy projection cannot establish live summarizer fidelity or speed.
    result = estimate(32, extra_summary_facts)

    assert result["summary_versions"] >= 5
    assert result["max_coordinator_input_tokens"] + 512 <= 8192
    assert result["max_summary_input_tokens"] + 1024 <= 8192


def test_planned_pair_leaves_admission_headroom() -> None:
    results = [estimate(turns) for turns in (39, 41)]

    # Reserve the full hundred-call runtime ceiling even after every projected
    # request. These estimates include tokenizer framing but exclude model retries,
    # retrievals and longer real outputs; the live usage ledger remains authoritative.
    assert sum(result["total_input_tokens"] for result in results) + 100 * 8192 <= 1500000
    assert sum(result["output_tokens"] for result in results) + 100 * 1024 <= 140000
    assert sum(result["model_calls"] for result in results) + 100 <= 800
