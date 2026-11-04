"""The live quality harness must stop before spending an unreserved model attempt."""

import asyncio

import pytest

from agent_harness.coordinator.model import ModelReply, ModelRequest
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import VisibleResult
from tests.quality.memory import MeasuredModel, decode_answer, quality_passes, select_question


def test_missing_usage_holds_reservation_and_stops_following_dispatch():
    class Unknown:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return ModelReply(answer="synthetic")

    source = Unknown()
    model = MeasuredModel(source, [], input_limit=100000, output_limit=10000, call_limit=2)
    request = ModelRequest((), (), max_output_tokens=100)
    asyncio.run(model.complete(request))
    assert model.input_tokens == model.counter(request)
    assert model.output_tokens == 100
    assert model.stopped

    with pytest.raises(RuntimeError, match="budget exhausted"):
        asyncio.run(model.complete(request))

    assert source.calls == 1


def test_known_usage_releases_unused_reservation_but_call_limit_remains():
    observations = []

    class Known:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            observations.append({"input_tokens": 12, "output_tokens": 2})
            return ModelReply(answer="synthetic")

    source = Known()
    model = MeasuredModel(source, observations, input_limit=100000, output_limit=102, call_limit=2)
    request = ModelRequest((), (), max_output_tokens=100)
    asyncio.run(model.complete(request))
    asyncio.run(model.complete(request))
    assert (model.input_tokens, model.output_tokens) == (24, 4)

    with pytest.raises(RuntimeError, match="budget exhausted"):
        asyncio.run(model.complete(request))

    assert source.calls == 2


def test_answer_parser_accepts_json_fences_but_rejects_non_object_answers():
    assert decode_answer('```json\n{"owner":null}\n```') == {"owner": None}

    with pytest.raises(ValueError):
        decode_answer('["owner"]')


def test_provider_usage_above_reserved_output_stops_next_dispatch():
    observations = []

    class Oversized:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            observations.append({"input_tokens": 12, "output_tokens": 101})
            return ModelReply(answer="synthetic")

    source = Oversized()
    model = MeasuredModel(source, observations, input_limit=100000, output_limit=500, call_limit=2)
    request = ModelRequest((), (), max_output_tokens=100)
    asyncio.run(model.complete(request))
    assert model.stopped and model.output_tokens == 101

    with pytest.raises(RuntimeError, match="budget exhausted"):
        asyncio.run(model.complete(request))

    assert source.calls == 1


def test_quality_gate_requires_both_repetitions_and_noncritical_threshold():
    case = {
        "id": "synthetic",
        "critical": ["amount"],
        "expected": {"amount": 5, **{f"detail-{index}": index for index in range(10)}},
    }
    rows = [
        {
            "case": "synthetic",
            "repetition": repetition,
            "critical_pass": True,
            "facts": {key: {"pass": True} for key in case["expected"]},
        }
        for repetition in range(2)
    ]
    record = {"cases": rows}
    assert quality_passes(record, [case])
    rows[0]["facts"]["detail-0"]["pass"] = False
    assert quality_passes(record, [case])
    rows[1]["facts"]["detail-0"]["pass"] = False
    assert not quality_passes(record, [case])
    rows[1]["facts"]["detail-0"]["pass"] = True
    rows[1]["repetition"] = 0
    assert not quality_passes(record, [case])


@pytest.mark.parametrize("failure", ["critical_pass", "retrieval_gate"])
def test_quality_gate_never_accepts_critical_or_required_retrieval_failure(failure):
    case = {"id": "synthetic", "critical": ["amount"], "expected": {"amount": 5}}
    rows = [
        {"case": "synthetic", "repetition": index, "critical_pass": True, "retrieval_gate": True}
        for index in range(2)
    ]
    rows[0][failure] = False
    assert not quality_passes({"cases": rows}, [case])


def test_model_evidence_records_only_matched_visible_history_retrieval_results():
    observations = []

    class Known:
        async def complete(self, request):
            observations.append({"input_tokens": 12, "output_tokens": 2})
            return ModelReply(answer="synthetic")

    model = MeasuredModel(
        Known(), observations, input_limit=100000, output_limit=1000, call_limit=1
    )
    request = ModelRequest(
        (
            ToolCall("history-1", "retrieve_history", {"after": 0, "limit": 5}),
            VisibleResult("history-1", {"events": [{"text": "The ribbon was ochre."}]}),
            VisibleResult("unmatched", {"text": "unmatched result"}),
            ToolCall("calculation-1", "calculate", {"values": [2, 3]}),
            VisibleResult("calculation-1", {"total": 5}),
        ),
        (),
        max_output_tokens=100,
    )
    asyncio.run(model.complete(request))
    assert model.calls[0]["retrieved_history"] == [{"events": [{"text": "The ribbon was ochre."}]}]


def test_retrieval_question_uses_an_actually_missing_predeclared_detail():
    case = {
        "retrieval_questions": [
            {"question": "First card?", "expected": {"code": "amber-51"}},
            {"question": "Second card?", "expected": {"code": "cedar-82"}},
        ]
    }
    history = [{"text": "First card: AMBER-51. Second card is no longer in this summary."}]
    assert select_question(case, history) == ("Second card?", {"code": "cedar-82"})

    with pytest.raises(ValueError, match="missing detail"):
        select_question(case, [{"text": "amber-51 and cedar-82"}])
