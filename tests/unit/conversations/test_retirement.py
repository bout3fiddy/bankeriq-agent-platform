"""Active memory shrinks only through explicit, source-bound corrections and answers."""

import json

import pytest

from agent_harness.memory.retirement import select_memory
from agent_harness.memory.semantic import MemoryFact, memory_message


def choose(facts, relation="supersedes"):
    return select_memory(
        json.dumps(
            {
                "entries": [{"candidate": 1, "label": "decision"}],
                "retirements": [{"previous": 0, "replacement": 1, "relation": relation}],
            }
        ),
        facts,
        facts,
    )


def test_repeated_explicit_corrections_bound_active_memory_and_link_sources():
    old = MemoryFact(1, 1, "user", "Budget: EUR 1200.", "decision")

    for number in range(2, 102):
        new = MemoryFact(number, number, "user", f"Budget correction: CHF {number * 100}.")
        selected = choose((old, new))
        assert len(selected.facts) == 1
        previous = selected.retirements[0]["previous"]
        replacement = selected.retirements[0]["replacement"]
        assert isinstance(previous, dict) and isinstance(replacement, dict)
        assert previous["source"] == old.source
        assert replacement["source"] == new.source
        old = selected.facts[0]

    assert "CHF 10100" in memory_message(selected.facts).text
    assert "EUR 1200" not in memory_message(selected.facts).text


@pytest.mark.parametrize(
    "new",
    [
        MemoryFact(2, 2, "user", "The payment completed."),
        MemoryFact(2, 2, "user", "Budget: CHF 1500."),
        MemoryFact(2, 2, "assistant", "Budget correction: CHF 1500."),
        MemoryFact(2, 2, "user", "Deadline correction: Friday."),
        MemoryFact(1, 1, "user", "Budget correction: CHF 1500."),
    ],
)
def test_unsupported_replacement_cannot_release_protected_fact(new):
    old = MemoryFact(1, 1, "user", "Budget: EUR 1200.", "decision")

    result = choose((old, new))
    assert old in result.facts
    assert not result.retirements


def test_explicit_answer_resolves_question_and_unrelated_claim_does_not():
    old = MemoryFact(1, 1, "user", "Which languages?", "open_question")
    answer = MemoryFact(3, 2, "user", "Languages confirmed: English and Dutch.")
    selection = choose((old, answer), "resolves")
    assert len(selection.facts) == 1
    assert selection.retirements[0]["relation"] == "resolves"

    result = choose((old, MemoryFact(3, 2, "user", "Payment confirmed.")), "resolves")
    assert old in result.facts and not result.retirements


def test_structured_labels_cannot_promote_claim_to_completed_action():
    facts = (MemoryFact(1, 1, "user", "I completed payment."),)

    result = select_memory('{"entries":[{"candidate":0,"label":"completed_action"}]}', facts, facts)
    assert result.facts[0].kind == "user" and result.facts[0].label == "fact"

    selected = select_memory('{"entries":[{"candidate":0,"label":"goal"}]}', facts, facts)
    sections = json.loads(memory_message(selected.facts).text.split("\n", 1)[1])
    assert sections["goals"][0]["kind"] == "user"
    assert not sections["completed_actions"]


def test_validated_retirement_wins_over_a_redundant_old_selection():
    facts = (
        MemoryFact(1, 1, "user", "Budget: EUR 1200.", "decision"),
        MemoryFact(2, 2, "user", "Budget correction: CHF 1500."),
    )
    selected = select_memory(
        json.dumps(
            {
                "entries": [
                    {"candidate": 0, "label": "decision"},
                    {"candidate": 1, "label": "fact"},
                ],
                "retirements": [{"previous": 0, "replacement": 1, "relation": "supersedes"}],
            }
        ),
        facts,
        facts,
    )
    assert len(selected.facts) == 1
    assert selected.facts[0].source == 2 and selected.facts[0].label == "decision"


def test_explicit_user_constraints_survive_selector_omission_without_action_authority():
    from agent_harness.memory.semantic import summary_candidates

    sources = (MemoryFact(1, 1, "user", "Constraint: use large print. Decision: pay tomorrow."),)
    candidates = summary_candidates(sources)
    result = select_memory('{"entries":[],"retirements":[]}', candidates, sources)
    assert {fact.label for fact in result.facts} == {"constraint", "decision"}
    assert all(fact.kind == "user" for fact in result.facts)
    assert all(fact.source == 1 for fact in result.facts)


def test_repeated_question_is_preserved_when_model_suggests_unsupported_retirement():
    facts = (
        MemoryFact(
            1, 1, "user", "Open question: choose an accessibility reviewer.", "open_question"
        ),
        MemoryFact(
            5, 4, "user", "Open question: we still need an accessibility reviewer.", "open_question"
        ),
    )
    result = choose(facts)
    assert facts[0] in result.facts and not result.retirements


def test_protected_excerpt_is_not_duplicated_when_model_changes_its_label():
    facts = (MemoryFact(1, 1, "user", "Budget: EUR 1200.", "decision"),)
    result = select_memory('{"entries":[{"candidate":0,"label":"fact"}]}', facts, facts)
    assert result.facts == facts


def test_user_names_and_owners_survive_repeated_selector_omission():
    from agent_harness.memory.semantic import summary_candidates

    sources = (
        MemoryFact(1, 1, "user", "Mara owns logistics; the sensor is nicknamed Copper Finch."),
        MemoryFact(2, 1, "assistant", "The sensor is called LANTERN-7B."),
    )
    result = select_memory('{"entries":[]}', summary_candidates(sources), sources)
    assert len(result.facts) == 1
    assert result.facts[0].kind == "user" and result.facts[0].label == "identity"
    assert result.facts[0].text == sources[0].text
    again = select_memory('{"entries":[]}', summary_candidates(result.facts), result.facts)
    assert again.facts == result.facts


def test_explicit_name_correction_can_retire_a_protected_identity():
    facts = (
        MemoryFact(1, 1, "user", "Prototype nickname: Copper Finch.", "identity"),
        MemoryFact(
            3, 2, "user", "Prototype nickname correction: Silver Wren replaces Copper Finch."
        ),
    )
    result = choose(facts)
    assert len(result.facts) == 1 and result.facts[0].source == 3
    assert result.facts[0].label == "identity"
    assert result.retirements[0]["previous"] == {
        "source": 1,
        "turn": 1,
        "text": facts[0].text,
    }


def test_confirmed_user_answer_survives_omission_without_claiming_tool_completion():
    from agent_harness.memory.semantic import summary_candidates

    sources = (
        MemoryFact(1, 1, "user", "Open question: who is the accessibility reviewer?"),
        MemoryFact(2, 2, "user", "Accessibility reviewer confirmed: Noor."),
        MemoryFact(3, 2, "user", "Payment confirmed, according to my colleague."),
    )
    result = select_memory('{"entries":[]}', summary_candidates(sources), sources)
    assert {fact.source for fact in result.facts} == {1, 2, 3}
    assert result.facts[1].label == "decision" and result.facts[1].kind == "user"
    assert all(fact.label != "completed_action" for fact in result.facts)
