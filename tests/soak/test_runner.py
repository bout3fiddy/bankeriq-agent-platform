"""Synthetic-clock coverage proves harness decisions without claiming elapsed endurance."""

import asyncio
import base64
import json
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest

from agent_platform.client.stream import Frame
from tests.soak.runner import Session, SoakBudget, SoakPlan, run_soak
from tests.soak.transport import CliTokens, HttpSoakClient, observe_frame


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class FakeClient:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sessions: list[Session] = []
        self.last_activity: dict[str, float] = {}
        self.calls: list[tuple[float, str, str | None]] = []
        self.cache_stays_alive = False
        self.summary_versions = 5
        self.watched: list[str] = []

    async def create(self) -> Session:
        session = Session(str(UUID(int=len(self.sessions) + 1)))
        self.sessions.append(session)
        return session

    async def turn(self, session: Session, text: str, expected: str | None) -> dict[str, Any]:
        self.last_activity[session.conversation] = self.clock.now
        self.calls.append((self.clock.now, session.conversation, expected))
        return {"status": "completed", "correct": True}

    async def inspect(self, session: Session) -> dict[str, Any]:
        age = self.clock.now - self.last_activity.get(session.conversation, self.clock.now)
        ttl = max(-2, int((1800 - age) * 1000))
        summaries = min(self.summary_versions, session.turns // 2)
        calls = session.turns + max(0, summaries)
        return {
            "cursor": session.turns * 2,
            "active_run": None,
            "durable": {"runs": session.turns, "journal_events": session.turns * 2},
            "cache": {
                "available": True,
                "idle_timeout_seconds": 1800,
                "view_ttl_ms": 1800000 if self.cache_stays_alive else ttl,
            },
            "summary_versions": summaries,
            "usage": {
                "model_calls": calls,
                "input_tokens": calls * 100,
                "output_tokens": calls * 10,
                "unknown_usage": 0,
                "expected_calls_lower_bound": calls,
            },
        }

    async def watch(self, session: Session, observations: dict[str, Any]) -> None:
        self.watched.append(session.conversation)
        started = self.clock.now
        observations["connections"] += 1

        try:
            await asyncio.Event().wait()
        finally:
            observations["connected_seconds"] += self.clock.now - started


def exercise(client: FakeClient, path: Path, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        run_soak(
            client,
            SoakPlan(**kwargs),
            ("Synthetic draft discussion.",),
            path,
            clock=lambda: client.clock.now,
            sleep=client.clock.sleep,
        )
    )


def test_full_schedule_has_passive_idle_and_independent_recall(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    result = exercise(client, tmp_path / "soak.json")

    assert result["status"] == "passed"
    assert result["elapsed_seconds"] == 7200
    assert result["time_source"] == "injected_test_clock"
    assert result["idle_seconds"] == 3900
    assert not any(1200 <= elapsed < 5100 for elapsed, _, _ in client.calls)
    assert client.watched == [client.sessions[0].conversation]
    recalled = [expected for elapsed, _, expected in client.calls if elapsed >= 5100 and expected]
    assert recalled == ["SOAK-CANARY-1", "SOAK-CANARY-2"] * 2
    assert result["checks"]["recall_after_soak"]
    assert len({attempt["run"] for attempt in result["attempts"]}) == len(result["attempts"])
    assert not (tmp_path / "soak.json.tmp").exists()


def test_open_stream_keeping_cache_alive_fails_the_gate(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    client.cache_stays_alive = True
    result = exercise(client, tmp_path / "soak.json")

    assert result["status"] == "failed"
    assert not result["checks"]["cache_expired"]


def test_missing_compaction_measurement_cannot_pass(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    client.summary_versions = -1
    result = exercise(client, tmp_path / "soak.json")

    assert result["status"] == "failed"
    assert not result["checks"]["repeated_compaction"]
    assert "summary_publication_count_unavailable" in result["limits"]


def test_budget_stops_admissions_and_keeps_stable_run_ids(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    result = exercise(client, tmp_path / "soak.json", max_turns=4)

    assert result["reason"] == "turn_budget_exhausted"
    assert len(client.calls) == len(result["attempts"]) == 4
    assert all(attempt["run"] for attempt in result["attempts"])


def test_existing_output_is_preserved_before_network_work(tmp_path: Path) -> None:
    path = tmp_path / "soak.json"
    path.write_text("earlier evidence")
    client = FakeClient(Clock())

    with pytest.raises(FileExistsError):
        exercise(client, path)

    assert path.read_text() == "earlier evidence"
    assert not client.sessions


def test_stop_request_prevents_setup_mutations(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    result = asyncio.run(
        run_soak(
            client,
            SoakPlan(),
            ("Synthetic draft.",),
            tmp_path / "soak.json",
            stopped=lambda: True,
        )
    )

    assert result["reason"] == "operator_stop"
    assert not client.sessions


def test_transport_error_preserves_attempt_without_payload(tmp_path: Path) -> None:
    class FailedClient(FakeClient):
        async def turn(self, session, text, expected):
            assert json.loads((tmp_path / "soak.json").read_text())["attempts"][-1]["run"]
            raise ValueError("PRIVATE_PAYLOAD_MUST_NOT_ENTER_EVIDENCE")

    result = exercise(FailedClient(Clock()), tmp_path / "soak.json")
    assert result["status"] == "failed"
    assert len(result["attempts"]) == 1
    assert "PRIVATE_PAYLOAD" not in (tmp_path / "soak.json").read_text()


def test_history_failure_after_terminal_status_is_not_success(tmp_path: Path) -> None:
    class FailedHistory(FakeClient):
        async def turn(self, session, text, expected):
            return {"status": "completed", "correct": None, "error": "transport_failure"}

    result = exercise(FailedHistory(Clock()), tmp_path / "soak.json")
    assert result["status"] == "stopped"
    assert result["reason"] == "turn_failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_seconds": 60},
        {"idle_seconds": 3600},
        {"max_turns": 1000},
        {"cadence_seconds": 0},
        {"min_summary_versions": 0},
    ],
)
def test_short_or_unbounded_runs_cannot_claim_endurance(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SoakPlan(**changes)


class Tokens(CliTokens):
    def __init__(self) -> None:
        self.refreshes = 1

    async def token(self) -> str:
        return "synthetic-token"

    def stream_seconds(self) -> float:
        return 600


def test_http_boundary_discards_extra_payloads_and_preserves_request_id() -> None:
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)

        if request.url.path == "/runs":
            return httpx2.Response(202, json={"accepted": True})

        if request.url.path == "/status":
            return httpx2.Response(200, json={"status": "completed", "private": "PRIVATE"})

        if request.url.path == "/history":
            return httpx2.Response(
                200,
                json={
                    "events": [
                        {
                            "run_id": "run-1",
                            "payload": {
                                "type": "Message",
                                "value": {"role": "assistant", "text": "READY"},
                            },
                        }
                    ]
                },
            )

        return httpx2.Response(
            200,
            json={
                "cursor": 2,
                "active_run": None,
                "durable": {"runs": 1, "journal_events": 2},
                "cache": {"available": True, "view_ttl_ms": 1700, "private": "PRIVATE"},
                "memory": {"summary_versions": 5, "summary_body": "PRIVATE"},
                "usage": {
                    "model_calls": 6,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "unknown_usage": 0,
                    "expected_calls_lower_bound": 6,
                    "private": "PRIVATE",
                },
                "private": "PRIVATE",
            },
        )

    async def exercise_http():
        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as http:
            client = HttpSoakClient(http, Tokens())
            session = Session("chat-1", "run-1")
            return await client.turn(session, "Synthetic prompt", "READY"), await client.inspect(
                session
            )

    turn, inspection = asyncio.run(exercise_http())
    assert turn["correct"]
    assert inspection["summary_versions"] == 5
    assert inspection["usage"]["model_calls"] == 6
    assert "PRIVATE" not in json.dumps((turn, inspection))
    assert json.loads(requests[0].content)["request_id"] == "run-1"
    assert all(request.headers["x-run-id"] == "run-1" for request in requests)
    assert all(request.url.path != "/activity" for request in requests)


def test_cli_credentials_refresh_after_idle_without_writing_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = Clock()
    user, tenant, audience = (str(UUID(int=value)) for value in (11, 12, 13))
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            claims = {"tid": tenant, "oid": user, "scp": "runtime.invoke", "exp": clock.now + 3600}
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return json.dumps({"accessToken": "header." + payload + ".signature"}).encode(), b""

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs["env"]["AZURE_CONFIG_DIR"]))
        return Process()

    monkeypatch.setattr("tests.soak.transport.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("tests.soak.transport.time.time", lambda: clock.now)
    tokens = CliTokens(audience, tenant, user, tmp_path)

    async def collect():
        first = await tokens.token()
        assert await tokens.token() == first
        assert tokens.stream_seconds() == 600
        clock.now += 3300
        assert tokens.stream_seconds() == 240
        clock.now -= 3300
        clock.now += 3900
        assert await tokens.token() != first

    asyncio.run(collect())
    assert tokens.refreshes == len(calls) == 2
    assert all(directory == str(tmp_path) for _, directory in calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "cursor",
    [
        "not-json",
        "[]",
        '{"sequence":3}',
        '{"recovery_id":"a","sequence":true,"deletion_generation":0}',
        "x" * 513,
    ],
)
def test_malformed_stream_cursors_cannot_be_replayed(cursor: str) -> None:
    with pytest.raises(ValueError):
        observe_frame(Frame("text", {}, cursor), {"events": {}})


def test_stream_counter_discards_payload_and_unknown_event_text() -> None:
    observations = {"events": {}}
    observe_frame(Frame("PRIVATE_MARKER", {"text": "SECRET_BODY"}), observations)
    assert observations["events"] == {"other": 1}
    assert "PRIVATE" not in json.dumps(observations)
    assert "SECRET" not in json.dumps(observations)


def test_idle_stream_cancellation_closes_reader_without_retaining_text() -> None:
    async def exercise_stream():
        observed = asyncio.Event()
        closed = asyncio.Event()

        async def chunks():
            try:
                yield b'event: text\ndata: {"text":"PRIVATE_BODY"}\n\n'
                observed.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        def respond(request):
            return httpx2.Response(
                200, content=chunks(), headers={"content-type": "text/event-stream"}
            )

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as http:
            metrics = {"connections": 0, "connected_seconds": 0.0, "events": {}, "errors": 0}
            task = asyncio.create_task(
                HttpSoakClient(http, Tokens()).watch(Session("a", "b"), metrics)
            )
            await asyncio.wait_for(observed.wait(), timeout=1)
            task.cancel()

            with suppress(asyncio.CancelledError):
                await task

            assert closed.is_set()
            assert metrics["events"] == {"text": 1}
            assert metrics["errors"] == 0
            assert "PRIVATE" not in json.dumps(metrics)

    asyncio.run(exercise_stream())


@pytest.mark.parametrize("reauthorize", [False, True])
def test_stream_eof_distinguishes_planned_reauthorization(reauthorize: bool) -> None:
    async def exercise_eof():
        ended = asyncio.Event()

        async def chunks():
            yield (
                b'event: reconnect\ndata: {"reason":"reauthorize"}\n\n'
                if reauthorize
                else b"event: view\ndata: {}\n\n"
            )
            ended.set()

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid",
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=chunks(), headers={"content-type": "text/event-stream"}
                )
            ),
        ) as http:
            metrics = {"connections": 0, "connected_seconds": 0.0, "events": {}, "errors": 0}
            task = asyncio.create_task(
                HttpSoakClient(http, Tokens()).watch(Session("a", "b"), metrics)
            )
            await asyncio.wait_for(ended.wait(), timeout=1)
            task.cancel()

            with suppress(asyncio.CancelledError):
                await task

            assert metrics["connections"] == 1
            assert metrics["errors"] == (0 if reauthorize else 1)

            if reauthorize:
                assert metrics["planned_reconnects"] == 1

    asyncio.run(exercise_eof())


def test_fragmented_oversized_stream_frame_stops_the_reader() -> None:
    async def exercise_oversize():
        async def chunks():
            yield b"x" * 131073
            yield b"x" * 131073

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid",
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=chunks(), headers={"content-type": "text/event-stream"}
                )
            ),
        ) as http:
            metrics = {"connections": 0, "connected_seconds": 0.0, "events": {}, "errors": 0}

            with pytest.raises(ValueError, match="frame exceeds"):
                await HttpSoakClient(http, Tokens()).watch(Session("a", "b"), metrics)

    asyncio.run(exercise_oversize())


def test_client_cancellation_keeps_the_admitted_identity(tmp_path: Path) -> None:
    class Interrupted(FakeClient):
        async def turn(self, session, text, expected):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        exercise(Interrupted(Clock()), tmp_path / "soak.json")

    record = json.loads((tmp_path / "soak.json").read_text())
    assert record["status"] == "stopped"
    assert record["reason"] == "client_interrupted"
    assert record["attempts"][0]["run"]


def test_budget_reserves_headroom_before_each_admission(tmp_path: Path) -> None:
    client = FakeClient(Clock())
    budget = SoakBudget(input_tokens=819300)
    result = exercise(client, tmp_path / "soak.json", budget=budget)

    assert result["reason"] == "usage_budget_exhausted"
    assert len(client.calls) == 2
    assert result["usage"]["input_tokens"] == 200
    assert result["reserved_usage"] == {}


@pytest.mark.parametrize("defect", ["missing", "deficit", "unknown", "pending"])
def test_unreconciled_usage_stops_the_next_admission(tmp_path: Path, defect: str) -> None:
    class IncompleteUsage(FakeClient):
        async def inspect(self, session):
            observation = await super().inspect(session)

            if session.turns:
                if defect == "missing":
                    observation["usage"] = None
                elif defect == "deficit":
                    observation["usage"]["model_calls"] = 0
                elif defect == "unknown":
                    observation["usage"]["unknown_usage"] = 1
                else:
                    observation["active_run"] = session.run

            return observation

    client = IncompleteUsage(Clock())
    result = exercise(client, tmp_path / "soak.json")

    assert result["reason"] == "usage_incomplete"
    assert result["elapsed_seconds"] == 5
    assert len(client.calls) == 1
    assert not result["checks"]["usage_reconciled"]
    assert result["reserved_usage"]["model_calls"] == 100


def test_delayed_usage_writer_reconciles_without_another_model_call(tmp_path: Path) -> None:
    class DelayedUsage(FakeClient):
        async def inspect(self, session):
            observation = await super().inspect(session)

            if session.turns and self.clock.now - self.last_activity[session.conversation] < 2:
                observation["usage"]["model_calls"] -= 1

            return observation

    client = DelayedUsage(Clock())
    result = exercise(client, tmp_path / "soak.json")

    assert result["status"] == "passed"
    assert result["checks"]["usage_reconciled"]
    assert len(client.calls) == len(result["attempts"])
    assert client.calls[1][0] >= 2
    assert any(
        item["usage"]["model_calls"] < item["usage"]["expected_calls_lower_bound"]
        for item in result["observations"]
    )


def test_usage_regression_cannot_make_more_budget_available(tmp_path: Path) -> None:
    class RegressedUsage(FakeClient):
        async def inspect(self, session):
            observation = await super().inspect(session)

            if session.turns >= 2:
                observation["usage"]["input_tokens"] = 0

            return observation

    client = RegressedUsage(Clock())
    result = exercise(client, tmp_path / "soak.json")

    assert result["reason"] == "usage_counter_regressed"
    assert len(client.calls) == 3
    assert not result["checks"]["usage_reconciled"]


@pytest.mark.parametrize(
    "tokens,reason",
    [
        (900000, "usage_reservation_exceeded"),
        (1200001, "usage_budget_exceeded"),
    ],
)
def test_observed_budget_overruns_stop_with_numeric_evidence(
    tmp_path: Path, tokens: int, reason: str
) -> None:
    class Overrun(FakeClient):
        async def inspect(self, session):
            observation = await super().inspect(session)
            observation["usage"]["input_tokens"] = session.turns * tokens
            return observation

    client = Overrun(Clock())
    result = exercise(client, tmp_path / "soak.json")

    assert result["reason"] == reason
    assert result["usage"]["input_tokens"] == tokens
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"model_calls": 1001},
        {"input_tokens": 2000001},
        {"output_tokens": 150001},
        {"reserve_calls": 101},
        {"reserve_calls": 0},
        {"model_calls": 99},
    ],
)
def test_invalid_shares_and_underfunded_reservations_fail_before_calls(changes) -> None:
    with pytest.raises(ValueError):
        SoakBudget(**changes)
