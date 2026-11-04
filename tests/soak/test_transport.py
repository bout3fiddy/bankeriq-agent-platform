"""Late-history recall and passive reconnect regressions for the operator harness."""

import asyncio
import json
from contextlib import suppress

import httpx2
import pytest

from agent_runtime.storage.history import recent_records
from tests.soak.runner import Session
from tests.soak.test_runner import Tokens
from tests.soak.transport import HttpSoakClient


@pytest.mark.parametrize("include_current_answer", [True, False])
def test_recall_uses_current_run_beyond_the_first_history_page(
    include_current_answer: bool,
) -> None:
    records = [
        {
            "sequence": index,
            "admission_sequence": index,
            "run_id": f"earlier-{index}",
            "payload": {
                "type": "Message",
                "value": {"role": "assistant", "text": "SOAK-CANARY-1"},
            },
        }
        for index in range(1, 301)
    ]

    if include_current_answer:
        records.append(
            {
                "sequence": 301,
                "admission_sequence": 301,
                "run_id": "current-recall",
                "payload": {
                    "type": "Message",
                    "value": {"role": "assistant", "text": "SOAK-CANARY-1"},
                },
            }
        )

    # Use the real bounded view selector: old records fall out while the newest
    # complete turn remains after index 100. All old answers deliberately match
    # the expected code, so accepting another run would produce a false pass.
    view = recent_records(records)
    assert len(view) == 200
    paths = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)

        if request.url.path == "/runs":
            return httpx2.Response(202, json={"accepted": True})

        if request.url.path == "/status":
            return httpx2.Response(200, json={"status": "completed"})

        assert request.url.path == "/history"
        return httpx2.Response(200, json={"events": view, "context_complete": True})

    async def exercise():
        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as http:
            return await HttpSoakClient(http, Tokens()).turn(
                Session("soak-chat", "current-recall"), "Recall the code.", "SOAK-CANARY-1"
            )

    result = asyncio.run(exercise())
    assert result["correct"] is include_current_answer
    assert result["answer_count"] == int(include_current_answer)
    assert paths == ["/runs", "/status", "/history"]
    assert "SOAK-CANARY" not in json.dumps(result)


def test_idle_reconnect_keeps_durable_cursor_and_only_uses_events() -> None:
    async def exercise():
        requests = []
        connected_again = asyncio.Event()
        cursor = json.dumps(
            {"recovery_id": "synthetic-generation", "deletion_generation": 0, "sequence": 301},
            separators=(",", ":"),
        )

        async def initial_frames():
            yield (
                f'id: {cursor}\nevent: saved\ndata: {{"items":[]}}\n\n'.encode()
                + b'event: final\ndata: {"answer":"PRIVATE_ANSWER"}\n\n'
                + b'event: reconnect\ndata: {"reason":"reauthorize"}\n\n'
            )

        async def resumed_frames():
            yield b": keepalive\n\n"
            connected_again.set()
            await asyncio.Event().wait()

        def respond(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(
                200,
                content=initial_frames() if len(requests) == 1 else resumed_frames(),
                headers={"content-type": "text/event-stream"},
            )

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as http:
            metrics = {"connections": 0, "connected_seconds": 0.0, "events": {}, "errors": 0}
            task = asyncio.create_task(
                HttpSoakClient(http, Tokens()).watch(Session("soak-chat", "idle-run"), metrics)
            )

            try:
                await asyncio.wait_for(connected_again.wait(), timeout=3)
            finally:
                task.cancel()

                with suppress(asyncio.CancelledError):
                    await task

        assert len(requests) == 2
        assert all(
            request.method == "GET" and request.url.path == "/events" for request in requests
        )
        assert "last-event-id" not in requests[0].headers
        assert requests[1].headers["last-event-id"] == cursor
        assert all(request.headers["x-run-id"] == "idle-run" for request in requests)
        assert metrics["planned_reconnects"] == 1
        assert metrics["errors"] == 0
        assert "PRIVATE_ANSWER" not in json.dumps(metrics)

    asyncio.run(exercise())
