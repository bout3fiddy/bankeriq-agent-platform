"""Finite client load retains accepted work until its model usage is reconciled."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

import httpx2

from performance.workloads import concurrent_answers
from tests.scaling.conversation_workload import cohort


def inspection():
    return {
        "database_time": datetime.now(UTC).isoformat(),
        "usage": {
            "model_calls": 1,
            "input_tokens": 100,
            "output_tokens": 1,
            "model_elapsed_ms": 1,
            "unknown_usage": 0,
        },
    }


def test_stream_rejection_holds_slot_through_recovery_without_resubmission(tmp_path):
    async def run():
        created, admitted, subscriptions = [], [], {}
        observed = []

        async def respond(request):
            if request.url.path == "/conversations":
                created.append(str(len(created)))
                return httpx2.Response(200, json={"conversation_id": created[-1]})

            if request.url.path == "/inspect":
                return httpx2.Response(200, json=inspection())

            run = request.headers["x-run-id"]

            if request.url.path == "/runs":
                admitted.append(run)
                return httpx2.Response(202, json={"run_id": run})

            assert request.url.path == "/events"
            subscriptions[run] = subscriptions.get(run, 0) + 1

            if run != admitted[0]:
                if subscriptions[run] == 1:
                    return httpx2.Response(429)

                # The next offered arrival occurs while the accepted run is
                # still recovering. It must remain backlog, with no second POST.
                await asyncio.sleep(0.05)

            final = {"run_id": run, "status": "completed", "answer": "READY", "provisional": False}
            return httpx2.Response(200, content="event: final\ndata: " + json.dumps(final) + "\n\n")

        async def headers():
            return {}

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as client:
            record = await cohort(
                client,
                headers=headers,
                output=tmp_path / "cohort.json",
                sessions=1,
                count=2,
                interval=0.01,
                wait_seconds=1,
                expected="READY",
                on_result=lambda row: observed.append(dict(row)),
                worker_budget=8,
            )

        assert len(admitted) == len(set(admitted)) == 2
        rejected, backlog = record["attempts"][1:]
        assert rejected["http_status"] == 429 and rejected["status"] == "accepted"
        assert rejected["recovery"] == {"status": "completed", "correct": True}
        assert backlog["status"] == "client_backlog"
        assert observed[-1]["usage"]["model_calls"] == 1
        assert record["peak_active"] == 1

    asyncio.run(run())


def test_uncertain_admission_stops_future_calls_and_retains_missing_usage(tmp_path, monkeypatch):
    admitted = []

    async def respond(request):
        if request.url.path == "/conversations":
            return httpx2.Response(200, json={"conversation_id": "synthetic"})

        if request.url.path == "/inspect":
            return httpx2.Response(200, json=inspection())

        assert request.url.path == "/runs"
        admitted.append(request.headers["x-run-id"])
        raise httpx2.ReadTimeout("synthetic missing admission acknowledgement", request=request)

    class Tokens:
        def __init__(self, *args):
            pass

        async def token(self):
            return "synthetic"

    real_client = httpx2.AsyncClient
    monkeypatch.setattr(concurrent_answers, "CliTokens", Tokens)
    monkeypatch.setattr(
        concurrent_answers.httpx2,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx2.MockTransport(respond)),
    )
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"audience": "synthetic", "url": "https://synthetic.invalid"}))
    args = argparse.Namespace(
        config=config,
        output=tmp_path / "result.json",
        tenant="synthetic",
        user="synthetic",
        sessions=1,
        count=1,
        stop=tmp_path / "STOP",
        worker_budget=8,
        max_output_tokens=5,
        budget_input_tokens=100000,
        budget_output_tokens=10000,
        control=True,
        image_digest="synthetic",
    )
    assert asyncio.run(concurrent_answers.main(args)) == 1
    record = json.loads(args.output.read_text())
    assert len(admitted) == 1
    assert record["anomalies"] == [{"index": 0, "reason": "usage_missing"}]
    assert record["attempts"][1]["status"] == "campaign_stopped"
    assert "usage" not in record["attempts"][0]
