"""Real Redis and PostgreSQL loss/race tests with isolated synthetic schema and tenant scopes."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    WaitReason,
)
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.storage.history import PostgresHistory, ProjectionPolicy
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate

from . import test_postgres
from .test_postgres import INPUT, Fault, save
from .test_postgres import context as context
from .test_postgres import store as store

primary_dsn = test_postgres.dsn


@pytest.fixture
def redis_url() -> str:
    value = os.environ.get("BANKERIQ_TEST_REDIS_URL")

    if not value:
        pytest.skip("set BANKERIQ_TEST_REDIS_URL to a disposable local Redis")

    return value


@pytest.fixture
def dsn(primary_dsn: str, redis_url: str):
    schema = "sessions_" + uuid4().hex

    with psycopg.connect(primary_dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    isolated = make_conninfo(primary_dsn, options=f"-c search_path={schema}")
    migrate(isolated)

    try:
        yield isolated
    finally:
        with psycopg.connect(primary_dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def sessions(store: PostgresStateStore, redis_url: str):
    cache = RedisSessions(redis_url)

    try:
        yield SessionService(store, cache)
    finally:
        cache.close()


def work(dsn: str, context: RunContext) -> dict[str, Any]:
    with psycopg.Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM projection_work WHERE tenant_id=%s AND conversation_id=%s",
            (context.tenant_id, context.conversation_id),
        ).fetchone()
        assert row is not None
        return row


def age(dsn: str, context: RunContext) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE conversations SET last_activity=now()-interval '31 minutes' "
            "WHERE tenant_id=%s AND conversation_id=%s",
            (context.tenant_id, context.conversation_id),
        )


def test_provisional_admission_restart_and_reconciliation(
    sessions: SessionService, context: RunContext, dsn: str, redis_url: str
) -> None:
    assert (
        sessions.stage(context, request_id="request", text="permitted input")["accepted"] is False
    )

    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)

    with pytest.raises(StateConflictError):
        sessions.stage(context, request_id="request", text="changed input")

    def crash(phase: str) -> None:
        if phase == "after_admission":
            raise RuntimeError("process stopped after commit")

    failing = SessionService(sessions.store, sessions.cache, fault=crash)

    with pytest.raises(RuntimeError, match="after commit"):
        failing.admit(context, request_id="request", text="permitted input")

    # A fresh interpreter has no inherited objects. It finds accepted input and
    # queued work together, reconciles the Redis-only pending record and reads
    # the same view again after deleting only this tenant's disposable cache key.
    script = """
import os, sys
from agent_runtime.runs.context import RunContext
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
c=RunContext(sys.argv[1], 'user', 'chat', 'run')
s=SessionService(PostgresStateStore(os.environ['SESSION_DSN']),
                 RedisSessions(os.environ['SESSION_REDIS']))
v=s.read(c, minimum_cursor=1)
assert v['events'][0]['payload']['value']['text']=='permitted input'
assert v['pending']==[]
with s.store._connect() as db:
    assert db.execute("SELECT status FROM jobs").fetchone()['status']=='ready'
s.cache.client.delete(s.cache.key(s.history.head(c)))
assert s.read(c, minimum_cursor=1)['events']==v['events']
"""
    result = subprocess.run(
        [sys.executable, "-c", script, context.tenant_id],
        env={**os.environ, "SESSION_DSN": dsn, "SESSION_REDIS": redis_url},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    accepted = sessions.admit(context, request_id="request", text="permitted input")
    assert accepted["accepted"] and accepted["cursor"] == 1

    with pytest.raises(StateConflictError):
        sessions.admit(context, request_id="request", text="conflicting reuse")


def test_warm_reads_primary_authority_and_queued_context(
    sessions: SessionService, context: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = sessions.admit(context, request_id="a", text="first")
    next_context = replace(context, run_id="second")
    second = sessions.admit(next_context, request_id="b", text="second")
    view = sessions.read(context, minimum_cursor=second["cursor"])
    assert [r["payload"]["value"]["text"] for r in view["events"]] == ["first", "second"]
    assert sessions.store.load_run(context).conversation.items == (
        Message(MessageRole.USER, "first"),
    )
    assert sessions.admit(context, request_id="a", text="first")["cursor"] == first["cursor"]

    def unexpected(context: RunContext) -> dict:
        raise AssertionError("warm session must not reload history")

    monkeypatch.setattr(sessions.history, "view", unexpected)
    assert sessions.read(context)["events"] == view["events"]

    with pytest.raises(StateNotFoundError):
        sessions.read(replace(context, user_id="other"))

    with pytest.raises(StateNotFoundError):
        sessions.read(replace(context, tenant_id="other"))

    with pytest.raises(StateConflictError):
        sessions.read(context, minimum_cursor=999)


def test_stale_updates_concurrent_hydration_and_cache_loss(
    sessions: SessionService, context: RunContext, dsn: str, redis_url: str
) -> None:
    sessions.admit(context, request_id="first", text="first")
    old = sessions.history.view(context)
    sessions.admit(replace(context, run_id="second"), request_id="second", text="second")
    newest = sessions.read(context)
    assert sessions.publish(context, old) is None
    head = sessions.history.head(context)
    sessions.cache.client.delete(sessions.cache.key(head))
    barrier = Barrier(2)

    def hydrate(_: int) -> dict:
        client = SessionService(PostgresStateStore(dsn), RedisSessions(redis_url))
        barrier.wait(timeout=5)

        try:
            return client.read(context, minimum_cursor=newest["cursor"])
        finally:
            client.cache.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(hydrate, (1, 2)))

    assert all(view["events"] == newest["events"] for view in views)
    assert sessions.publish(context, old) is None
    assert sessions.read(context)["cursor"] == newest["cursor"]


def test_redis_outage_never_undoes_acceptance(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    cache = RedisSessions("redis://127.0.0.1:1/0")
    service = SessionService(store, cache)

    try:
        assert not service.stage(context, request_id="q", text="hello")["display_cached"]
        accepted = service.admit(context, request_id="q", text="hello")
        assert accepted["accepted"]
        assert service.read(context, minimum_cursor=accepted["cursor"])["events"]
        assert work(dsn, context)["dirty_cursor"] > work(dsn, context)["cache_cursor"]
    finally:
        cache.close()


def test_projection_rollback_lost_ack_and_snapshot_tail_equivalence(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    sessions.admit(context, request_id="a", text="first")

    for boundary in range(1, 4):
        failing = PostgresHistory(PostgresStateStore(dsn, fault=Fault(write=boundary)))

        with pytest.raises(RuntimeError, match="synthetic rollback"):
            failing.materialize(context)

        assert work(dsn, context)["projected_cursor"] == 0

        with psycopg.connect(dsn) as connection:
            assert connection.execute("SELECT count(*) FROM history_pages").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM conversation_snapshots").fetchone() == (
                0,
            )

    history = PostgresHistory(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    history.materialize(context)
    assert work(dsn, context)["projected_cursor"] == 1
    sessions.admit(replace(context, run_id="second"), request_id="b", text="second")
    assert work(dsn, context)["dirty_cursor"] == 2
    assert work(dsn, context)["projected_cursor"] == 1
    hydrated = sessions.history.view(context)

    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT payload FROM conversation_events ORDER BY sequence"
        ).fetchall()

    assert [row["payload"] for row in hydrated["events"]] == [row[0] for row in rows]
    assert sessions.history.page(context)["next_cursor"] == 1
    assert sessions.history.page(context, after=1)["events"] == hydrated["events"][1:]


def test_cache_update_crash_and_new_event_preserve_dirty_work(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    sessions.admit(context, request_id="a", text="first")

    def crash(phase: str) -> None:
        if phase == "after_cache_update":
            sessions.admit(replace(context, run_id="second"), request_id="b", text="second")
            raise RuntimeError("crash before acknowledgement")

    failing = SessionService(sessions.store, sessions.cache, fault=crash)

    with pytest.raises(RuntimeError, match="acknowledgement"):
        failing.project_due(ProjectionPolicy(events=1))

    assert work(dsn, context)["projected_cursor"] == 1
    assert work(dsn, context)["cache_cursor"] == 0
    assert work(dsn, context)["dirty_cursor"] == 2
    sessions.project_due(ProjectionPolicy(events=1))
    assert work(dsn, context)["projected_cursor"] == 2
    assert work(dsn, context)["cache_cursor"] == 2
    assert sessions.read(context)["cursor"] == 2


def test_idle_cleanup_and_reads_racing_new_turn(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    sessions.admit(context, request_id="a", text="first")
    age(dsn, context)
    head = sessions.history.head(context)
    observed = sessions.cache.apply("peek", head)
    assert observed is not None
    assert not sessions.cleanup(context, observed)
    sessions.project_due()
    observed = sessions.cache.apply("peek", head)
    assert observed is not None
    sessions.read(context)
    assert not sessions.cleanup(context, observed)
    observed = sessions.cache.apply("peek", head)
    assert observed is not None
    sessions.admit(replace(context, run_id="second"), request_id="b", text="second")
    assert not sessions.cleanup(context, observed)
    age(dsn, context)
    assert context.conversation_id in [c.conversation_id for c in sessions.history.idle()]
    sessions.project_due()
    key = sessions.cache.key(sessions.history.head(context))
    cached = json.loads(cast(bytes, sessions.cache.client.get(key)))
    cached["last_access"] = 0
    sessions.cache.client.set(key, json.dumps(cached))
    sessions.cache.client.set(key + ":activity:0", "0", ex=1800)
    assert sessions.cleanup_idle()[0] == 1
    assert sessions.cache.apply("peek", sessions.history.head(context)) is None
    assert len(sessions.read(context)["events"]) == 2


def test_deleted_and_restored_generations_reject_delayed_refill(
    sessions: SessionService, context: RunContext, dsn: str, redis_url: str
) -> None:
    sessions.admit(context, request_id="a", text="first")
    old = sessions.history.view(context)
    old_head = sessions.history.head(context)

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id=%s", (str(uuid4()),))

    with pytest.raises(StateConflictError, match="recovery"):
        sessions.read(context)

    restored = SessionService(PostgresStateStore(dsn), RedisSessions(redis_url))

    try:
        with pytest.raises(StateConflictError):
            restored.publish(context, old)

        restored.read(context)
        assert restored.cache.key(restored.history.head(context)) != sessions.cache.key(old_head)
        before_delete = restored.history.head(context)
        restored.store.delete_conversation(context)
        restored.project_due()
        restored.publish(context, old)

        with pytest.raises(StateNotFoundError):
            restored.read(context)

        tombstone = restored.cache.apply("peek", restored.history.head(context, allow_deleted=True))
        assert tombstone is not None and tombstone["deleted"]
        assert restored.cache.apply("publish", before_delete, view=json.dumps(old)) is None
    finally:
        restored.cache.close()


def test_wait_recovery_restricted_isolation_and_bounded_progress(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    checkpoint = sessions.store.admit(context, request_id="q", items=INPUT)
    checkpoint = save(sessions.store, checkpoint, RunStatus.RUNNING)
    checkpoint = save(
        sessions.store, checkpoint, RunStatus.RUNNING, items=(ToolCall("call", "report", {}),)
    )
    checkpoint = save(
        sessions.store,
        checkpoint,
        RunStatus.WAITING,
        pending_calls=(PendingToolCall("call", WaitReason.JOB, "job"),),
    )
    sessions.read(context)
    sessions.cache.client.delete(sessions.cache.key(sessions.history.head(context)))
    assert PostgresStateStore(dsn).load_run(context) == checkpoint
    sessions.read(context)
    save(
        sessions.store,
        checkpoint,
        RunStatus.RUNNING,
        items=(RestrictedReceipt("call"),),
        artifacts=(ArtifactReference("call", "private-link"),),
    )

    for sequence in range(1, 40):
        sessions.progress(context, sequence=sequence, status="running")

    sessions.project_due(ProjectionPolicy(events=1))
    view = sessions.read(context)
    assert len(view["progress"]) == 32
    assert "private-link" not in json.dumps(view)
    assert "private-link" not in json.dumps(sessions.history.page(context))
    assert any(row["payload"]["type"] == "RestrictedReceipt" for row in view["events"])

    with psycopg.connect(dsn) as connection:
        assert "private-link" not in str(
            connection.execute("SELECT payload FROM history_pages").fetchall()
        )


def test_busy_history_is_bounded_and_materialised_without_idle(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:

    # Eleven admissions of 100 permitted messages produce 1,100 events while
    # the first run remains active. Recent UI keeps complete last-two turns;
    # the active model still sees only its first admitted turn.
    for turn in range(11):
        current = replace(context, run_id=f"run-{turn}")
        items = tuple(
            Message(MessageRole.USER, f"turn {turn} item {index}") for index in range(100)
        )
        sessions.store.admit(current, request_id=f"q-{turn}", items=items)

    assert len(sessions.store.load_run(replace(context, run_id="run-0")).conversation.items) == 100
    assert len(sessions.history.due(ProjectionPolicy())) == 1

    for _ in range(11):
        sessions.project_due()

    assert work(dsn, context)["projected_cursor"] == 1100
    assert work(dsn, context)["cache_cursor"] == 1100
    assert work(dsn, context)["pending_events"] == 0
    assert sessions.history.due(ProjectionPolicy()) == []
    sessions.cache.client.delete(sessions.cache.key(sessions.history.head(context)))
    view = sessions.read(context)
    assert len(view["events"]) == 200
    assert {event["run_id"] for event in view["events"]} == {"run-9", "run-10"}
    cursor = 0
    all_events = []

    while cursor < view["cursor"]:
        page = sessions.history_page(context, after=cursor)
        assert cursor < page["next_cursor"] <= view["cursor"]
        all_events.extend(page["events"])
        cursor = page["next_cursor"]

    assert len(all_events) == 1100
    assert all_events[-200:] == view["events"]

    with psycopg.connect(dsn) as connection:
        bounds = connection.execute(
            "SELECT max(last_cursor-first_cursor), max(octet_length(payload::text)) "
            "FROM history_pages"
        ).fetchone()
        assert bounds is not None and bounds[0] <= 99 and bounds[1] <= 1048576
        assert connection.execute("SELECT count(*) FROM conversation_events").fetchone() == (1100,)


def test_byte_time_triggers_page_bounds_and_sliding_ttl(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    for index in range(22):
        sessions.store.admit(
            replace(context, run_id=f"run-{index}"),
            request_id=f"request-{index}",
            items=(Message(MessageRole.USER, "x" * 50000),),
        )

    pending = work(dsn, context)
    assert pending["pending_events"] < 100 and pending["pending_bytes"] >= 1048576
    assert len(sessions.history.due(ProjectionPolicy())) == 1
    sessions.project_due()
    partial = work(dsn, context)["projected_cursor"]
    assert 0 < partial < 22
    sessions.history.materialize(context)
    assert work(dsn, context)["projected_cursor"] == 22
    view = sessions.read(context)
    key = sessions.cache.key(sessions.history.head(context))
    sessions.cache.client.expire(key, 2)
    sessions.record_activity(context)
    sessions.read(context)
    assert cast(int, sessions.cache.client.ttl(key)) > 1700
    assert len(view["events"]) <= 5

    # Metadata-only changes have no pending payload bytes, so the time trigger
    # still discovers them; it uses PostgreSQL time and survives cache loss.
    sessions.store.admit(replace(context, run_id="last"), request_id="last", items=INPUT)

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE projection_work SET dirty_since=now()-interval '61 seconds'")

    assert len(sessions.history.due(ProjectionPolicy(events=10000, bytes=100000000))) == 1


def test_provisional_capacity_and_pending_loss_are_not_acceptance(
    sessions: SessionService, context: RunContext
) -> None:
    for index in range(8):
        sessions.stage(replace(context, run_id=f"r{index}"), request_id=f"q{index}", text="pending")

    with pytest.raises(StateConflictError, match="capacity"):
        sessions.stage(replace(context, run_id="overflow"), request_id="overflow", text="pending")

    assert len(sessions.read(context)["pending"]) == 8
    sessions.cache.client.delete(sessions.cache.key(sessions.history.head(context)))
    assert sessions.read(context)["pending"] == []
    assert sessions.read(context)["events"] == []


def test_two_tenants_reuse_conversation_and_request_names(
    sessions: SessionService, context: RunContext
) -> None:
    other = replace(context, tenant_id=str(uuid4()))
    sessions.store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )
    sessions.admit(context, request_id="same", text="tenant one")
    sessions.admit(other, request_id="same", text="tenant two")
    assert sessions.read(context)["events"][0]["payload"]["value"]["text"] == "tenant one"
    assert sessions.read(other)["events"][0]["payload"]["value"]["text"] == "tenant two"
    assert sessions.cache.key(sessions.history.head(context)) != sessions.cache.key(
        sessions.history.head(other)
    )


def test_projection_unknown_commit_does_not_consume_later_dirty_cursor(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    sessions.admit(context, request_id="a", text="first")
    lost = False

    def lose_ack(phase: str) -> None:
        nonlocal lost

        if phase == "after_commit" and not lost:
            lost = True
            sessions.admit(replace(context, run_id="second"), request_id="b", text="second")
            raise psycopg.OperationalError("synthetic projection acknowledgement loss")

    PostgresHistory(PostgresStateStore(dsn, fault=lose_ack)).materialize(context)
    assert work(dsn, context)["projected_cursor"] == 1
    assert work(dsn, context)["dirty_cursor"] == 2
    assert work(dsn, context)["pending_events"] == 1


def test_metadata_only_pages_advance_without_inventing_history(
    sessions: SessionService, context: RunContext
) -> None:
    checkpoint = sessions.store.admit(context, request_id="a", items=INPUT)
    checkpoint = save(sessions.store, checkpoint, RunStatus.RUNNING)
    sessions.project_due(ProjectionPolicy(events=1))
    before = sessions.history.head(context)["cursor"]
    save(sessions.store, checkpoint, RunStatus.RUNNING)
    sessions.project_due(ProjectionPolicy(seconds=1, events=1))
    sessions.history.materialize(context)
    page = sessions.history_page(context, after=before)
    assert page["events"] == []
    assert page["next_cursor"] == before + 1


def test_session_http_commands_admit_without_executing(
    sessions: SessionService, context: RunContext
) -> None:
    import asyncio

    import httpx2
    from starlette.requests import Request

    from agent_runtime.coordinator.runner import Coordinator
    from agent_runtime.durable.handler import ContinuationHandler
    from agent_runtime.hosting.access import InputRejected, Principal
    from agent_runtime.hosting.endpoint import RuntimeEndpoint
    from agent_runtime.prompts.resolution import FixedPromptResolver
    from agent_runtime.providers.foundry.hosting import create_hosted_server

    from .harness import RuntimeHarness

    runtime = RuntimeHarness()
    coordinator = Coordinator(
        sessions.store,
        runtime.registry,
        runtime.events,
        runtime.model,
        runtime.access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )

    def prepare(principal: Principal, text: str) -> str:
        if text != "approved":
            raise InputRejected("input policy rejected")

        return text

    endpoint = RuntimeEndpoint(
        sessions.store,
        coordinator,
        ContinuationHandler(sessions.store, coordinator, runtime.events),
        prepare_input=prepare,
        sessions=sessions,
    )

    async def authenticate(request: Request) -> Principal:
        return Principal(context.tenant_id, context.user_id)

    async def exercise() -> None:
        host = create_hosted_server(endpoint, authenticate=authenticate)

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=host), base_url="http://synthetic"
        ) as client:
            body = {
                "action": "stage_input",
                "conversation_id": context.conversation_id,
                "run_id": context.run_id,
                "request_id": "request",
                "message": "approved",
            }
            staged = await client.post("/invocations", json=body)
            assert staged.status_code == 200 and staged.json()["accepted"] is False
            denied = await client.post("/invocations", json={**body, "message": "private"})
            assert denied.status_code == 403
            spoofed = await client.post("/invocations", json={**body, "tenant_id": "other"})
            assert spoofed.status_code == 400
            admitted = await client.post("/invocations", json={**body, "action": "admit_input"})
            assert admitted.status_code == 200 and admitted.json()["accepted"] is True
            read = await client.post(
                "/invocations",
                json={
                    "action": "read_session",
                    "conversation_id": context.conversation_id,
                    "minimum_cursor": admitted.json()["cursor"],
                },
            )
            assert read.status_code == 200 and len(read.json()["events"]) == 1
            assert read.json()["pending"] == []
            history = await client.post(
                "/invocations",
                json={
                    "action": "read_history",
                    "conversation_id": context.conversation_id,
                },
            )
            assert history.status_code == 200 and len(history.json()["events"]) == 1

    asyncio.run(exercise())
    assert runtime.model.requests == []


def test_full_width_cursor_fences_are_not_rounded_by_lua(
    sessions: SessionService, context: RunContext
) -> None:
    head = sessions.history.head(context)
    newer = str(2**53 + 1)
    older = str(2**53)
    assert sessions.cache.apply("publish", head, cursor=newer, view="{}") is not None
    assert sessions.cache.apply("publish", head, cursor=older, view="{}") is None
    state = sessions.cache.apply("peek", head)
    assert state is not None and state["cursor"] == newer


def test_conflicting_provisional_display_reconciles_without_hiding_history(
    sessions: SessionService, context: RunContext
) -> None:
    sessions.stage(context, request_id="same", text="old provisional")
    sessions.store.admit(context, request_id="same", items=INPUT)
    view = sessions.read(context)
    assert view["pending"][0]["status"] == "conflict"
    assert view["events"][0]["payload"]["value"]["text"] == "synthetic input"
    assert sessions.read(context)["pending"] == []


def test_oversized_turn_commits_results_but_blocks_inference(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    from agent_runtime.conversations.context import ContextBudgetExceeded
    from agent_runtime.coordinator.runner import Coordinator
    from agent_runtime.prompts.resolution import FixedPromptResolver
    from agent_runtime.tools.results import VisibleResult

    from .harness import RuntimeHarness

    checkpoint = sessions.store.admit(context, request_id="large", items=INPUT)
    checkpoint = save(sessions.store, checkpoint, RunStatus.RUNNING)

    for index in range(6):
        call_id = f"call-{index}"
        checkpoint = save(
            sessions.store, checkpoint, RunStatus.RUNNING, items=(ToolCall(call_id, "lookup", {}),)
        )
        checkpoint = save(
            sessions.store,
            checkpoint,
            RunStatus.RUNNING,
            items=(VisibleResult(call_id, {"text": "x" * 50000}),),
        )

    assert not checkpoint.conversation.context_complete
    assert checkpoint.conversation.items == ()

    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM results").fetchone() == (6,)

    sessions.history.materialize(context)
    assert sessions.read(context)["context_complete"] is False
    assert len(sessions.history_page(context)["events"]) == 13
    runtime = RuntimeHarness()
    coordinator = Coordinator(
        sessions.store,
        runtime.registry,
        runtime.events,
        runtime.model,
        runtime.access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )

    with pytest.raises(ContextBudgetExceeded):
        coordinator.continue_run(checkpoint)

    assert runtime.model.requests == []


def test_projection_fairness_and_idle_keyset(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    first = replace(context, conversation_id="a")
    second = replace(context, conversation_id="b", run_id="second")
    sessions.store.create_conversation(
        tenant_id=first.tenant_id, user_id=first.user_id, conversation_id=first.conversation_id
    )
    sessions.store.create_conversation(
        tenant_id=second.tenant_id, user_id=second.user_id, conversation_id=second.conversation_id
    )
    sessions.admit(first, request_id="a", text="first")
    sessions.admit(second, request_id="b", text="second")
    unavailable = RedisSessions("redis://127.0.0.1:1/0")

    try:
        projector = SessionService(sessions.store, unavailable)
        assert projector.project_due(ProjectionPolicy(events=1), limit=1) == 1
        assert projector.project_due(ProjectionPolicy(events=1), limit=1) == 1
        assert work(dsn, first)["projected_cursor"] == 1
        assert work(dsn, second)["projected_cursor"] == 1
        assert work(dsn, first)["cache_cursor"] == 0
    finally:
        unavailable.close()

    age(dsn, first)
    age(dsn, second)
    sessions.cache.client.delete(sessions.cache.key(sessions.history.head(first)))
    removed, cursor = sessions.cleanup_idle(limit=1)
    assert removed == 0
    assert cursor is not None
    assert cursor == (context.tenant_id, "a")
    assert [c.conversation_id for c in sessions.history.idle(after=cursor)] == ["b"]

    # Background refresh must not make an idle session look recently read.
    head = sessions.history.head(second)
    key = sessions.cache.key(head)
    cached = json.loads(cast(bytes, sessions.cache.client.get(key)))
    cached["last_access"] = 0
    sessions.cache.client.set(key, json.dumps(cached))
    sessions.publish(second, sessions.history.view(second))
    assert sessions.cleanup_idle(after=cursor)[0] == 1


def test_shared_prompt_cache_loss_corruption_and_withdrawal(
    sessions: SessionService, dsn: str
) -> None:
    from agent_runtime.prompts.resolution import PromptScope, PromptUnavailableError
    from bankeriq_agent_platform.sessions.prompts import RedisPromptBodies
    from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver, PromptAdministration

    from .test_prompts import ADMIN, activate, publish

    scope = PromptScope(str(uuid4()), "test")
    admin = PromptAdministration(dsn, scope, authorize=lambda actor, scope: actor == ADMIN)
    publish(admin, "v1")
    activate(admin, "v1", 0)
    cache = RedisPromptBodies(sessions.cache)
    reader = PostgresPromptResolver(dsn, scope, body_cache=cache)
    manifest = reader.resolve()
    expected = reader.instructions(manifest)
    assert cache.get(manifest) == "Guidance v1"
    assert PostgresPromptResolver(dsn, scope, body_cache=cache).instructions(manifest) == expected
    sessions.cache.client.set(cache.key(manifest), "corrupted instructions")
    assert PostgresPromptResolver(dsn, scope, body_cache=cache).instructions(manifest) == expected
    sessions.cache.client.delete(cache.key(manifest))
    assert PostgresPromptResolver(dsn, scope, body_cache=cache).instructions(manifest) == expected
    admin.withdraw(ADMIN, request_id="withdraw", version="v1")

    with pytest.raises(PromptUnavailableError):
        reader.instructions(manifest)
