"""Mixed-load accounting preserves overlap, stopped arrivals and uncertain cost."""

import asyncio
from copy import deepcopy

from tests.mixed.runner import RESERVE, run_mixed
from tests.soak.runner import Session


class SyntheticClient:
    def __init__(self, *, unknown=False):
        self.values = {}
        self.calls = []
        self.active = self.peak = 0
        self.unknown = unknown

    async def create(self):
        session = Session(str(len(self.values)))
        self.values[session.conversation] = {
            "active_run": None,
            "summary_versions": 0,
            "usage": {
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "unknown_usage": 0,
                "expected_calls_lower_bound": 0,
            },
        }
        return session

    async def inspect(self, session):
        return deepcopy(self.values[session.conversation])

    async def watch(self, session, observations):
        raise AssertionError("mixed traffic opens run subscriptions only")

    async def turn(self, session, text, expected):
        self.calls.append(session.run)
        self.active += 1
        self.peak = max(self.peak, self.active)
        value = self.values[session.conversation]
        value["active_run"] = session.run
        await asyncio.sleep(0.001)
        self.active -= 1
        value["active_run"] = None
        usage = value["usage"]
        usage["model_calls"] += 1
        usage["expected_calls_lower_bound"] += 1
        usage["input_tokens"] += 20 if session.conversation == "0" else 1500
        usage["output_tokens"] += 2 if session.conversation == "0" else 67
        usage["unknown_usage"] = int(self.unknown)

        if session.conversation == "0" and usage["model_calls"] > 5:
            value["summary_versions"] += 1

        return {
            "status": "completed",
            "correct": True,
            "admission_seconds": 0.001,
            "first_text_seconds": 0.002,
            "turn_seconds": 0.003,
        }


def test_pairs_overlap_and_classify_actual_publications(tmp_path):
    client = SyntheticClient()
    record = asyncio.run(
        run_mixed(
            client,
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=3,
            interval=0.01,
            limits={key: value * 3 for key, value in RESERVE.items()},
        )
    )
    assert client.peak == 2
    assert len(client.calls) == len(set(client.calls)) == 11
    assert not record["reservations"] and not record["stopped"]
    assert record["usage"]["model_calls"] == 11
    assert record["summary"]["ordinary"]["count"] == 3
    assert record["summary"]["memory_one_compaction"]["count"] == 3
    assert record["summary"]["memory_without_compaction"]["count"] == 0


def test_actual_usage_prevents_reusing_the_full_initial_allocation(tmp_path):
    client = SyntheticClient()
    record = asyncio.run(
        run_mixed(
            client,
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=3,
            interval=0.01,
            limits=dict(RESERVE),
        )
    )
    assert len(client.calls) == 1
    assert record["usage"]["model_calls"] == 1
    assert record["stopped"] and not record["reservations"]
    scheduled = [row for row in record["attempts"] if not row["warmup"]]
    assert len(scheduled) == 6
    assert all(row["status"] == "campaign_stopped" for row in scheduled)


def test_unknown_usage_holds_full_reservation_and_never_resubmits(tmp_path):
    client = SyntheticClient(unknown=True)
    record = asyncio.run(
        run_mixed(
            client,
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=2,
            interval=0.01,
            limits={key: value * 3 for key, value in RESERVE.items()},
        )
    )
    assert len(client.calls) == 1
    assert record["usage"]["model_calls"] == 0
    assert record["reservations"] == {client.calls[0]: RESERVE}
    assert record["stopped"] and record["attempts"][0]["usage_unresolved"]


def test_missing_initial_usage_stops_before_admitting_any_work(tmp_path):
    class MissingUsage(SyntheticClient):
        async def inspect(self, session):
            value = await super().inspect(session)
            value["usage"] = None
            return value

    client = MissingUsage()
    record = asyncio.run(
        run_mixed(
            client,
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=1,
            interval=0.01,
            limits={key: value * 3 for key, value in RESERVE.items()},
        )
    )
    assert not client.calls and not record["reservations"]
    assert record["stopped"] and record["attempts"][0]["status"] == "setup_unresolved"


def test_missing_publication_observation_is_not_counted_as_zero(tmp_path):
    class MissingPublications(SyntheticClient):
        async def inspect(self, session):
            value = await super().inspect(session)
            value["summary_versions"] = -1
            return value

    client = MissingPublications()
    record = asyncio.run(
        run_mixed(
            client,
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=1,
            interval=0.01,
            limits={key: value * 3 for key, value in RESERVE.items()},
        )
    )
    assert len(client.calls) == 1
    assert record["stopped"] and record["attempts"][0]["summary_publications"] is None
    assert record["summary"]["memory_without_compaction"]["count"] == 0


def test_out_of_envelope_ordinary_reply_is_retained_but_not_qualified(tmp_path):
    class ShortReply(SyntheticClient):
        async def inspect(self, session):
            value = await super().inspect(session)

            if session.conversation != "0" and value["usage"]["model_calls"]:
                value["usage"]["output_tokens"] = 2

            return value

    record = asyncio.run(
        run_mixed(
            ShortReply(),
            tmp_path / "mixed.json",
            memory_text="synthetic",
            batches=1,
            interval=0.01,
            limits={key: value * 3 for key, value in RESERVE.items()},
        )
    )
    ordinary = next(row for row in record["attempts"] if row["lane"] == "ordinary")
    assert ordinary["correct"] and ordinary["workload_invalid"]
    assert record["stopped"] and record["summary"]["ordinary"]["count"] == 0
