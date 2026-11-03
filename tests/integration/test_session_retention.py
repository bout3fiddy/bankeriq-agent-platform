"""Real Redis expiry while authenticated readers and independent tools remain active."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import psycopg
import pytest

from agent_harness.runs.context import RunContext
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.sessions.stream import ProgressStream
from agent_runtime.storage.history import ProjectionPolicy

from .test_async_groups import fanout
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import age
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import expire, wait_file


def keys(sessions: SessionService, context: RunContext) -> tuple[str, str, str]:
    head = sessions.history.head(context)
    base = sessions.cache.key(head)
    generation = head["deletion_generation"]
    return base, f"{base}:progress:{generation}", f"{base}:activity:{generation}"


def test_tool_process_retains_cache_until_lease_expires(
    sessions: SessionService, context: RunContext, dsn: str, tmp_path: Path
) -> None:
    fanout(sessions.store, context)
    sessions.cache.ttl_seconds = 1
    original = sessions.read(context)
    sessions.progress(context, sequence=1, status="running")
    view_key, progress_key, activity_key = keys(sessions, context)
    payload = sessions.cache.client.get(view_key)
    marker, release = tmp_path / "claimed", tmp_path / "release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.integration.async_process",
            "tool",
            str(marker),
            str(release),
        ],
        env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_file(marker, child)

        # No reader or token update runs for more than two cache lifetimes. The
        # separate process renews its SQL tool lease; bounded maintenance alone
        # extends Redis retention without copying or changing the history body.
        deadline = time.monotonic() + 2.2

        while time.monotonic() < deadline:
            assert sessions.keep_active() == (1, None)
            assert sessions.cache.client.get(view_key) == payload
            assert sessions.cache.client.exists(progress_key)
            time.sleep(0.1)
    finally:
        child.kill()
        child.communicate(timeout=5)

    # Killing a worker leaves an expiring lease. A waiting run by itself cannot
    # hold cache space forever; both its payloads and activity marker expire.
    time.sleep(0.7)
    assert sessions.keep_active() == (0, None)
    time.sleep(0.5)
    assert sessions.cache.client.exists(view_key, progress_key, activity_key) == 0
    assert sessions.read(context)["events"] == original["events"]


def test_open_sse_and_status_reads_allow_idle_expiry(
    sessions: SessionService, context: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.cache.ttl_seconds = 1
    sessions.admit(context, request_id="input", text="synthetic")
    sessions.progress(context, sequence=1, status="running")
    view_key, progress_key, _ = keys(sessions, context)

    def forbidden(*args, **kwargs):
        raise AssertionError("keepalive accessed the cached transcript")

    monkeypatch.setattr(sessions.cache, "apply", forbidden)
    monkeypatch.setattr(sessions.store, "_recent_events", forbidden)
    stream = ProgressStream(sessions, context)
    deadline = time.monotonic() + 1.2

    while time.monotonic() < deadline:
        sessions.progress_view(context)
        stream.poll()

        while stream.buffer.take() is not None:
            pass

        time.sleep(0.1)

    assert sessions.cache.client.exists(view_key, progress_key) == 0


def test_explicit_activity_extends_retention_but_background_reads_do_not(
    sessions: SessionService, context: RunContext
) -> None:
    sessions.cache.ttl_seconds = 1
    sessions.admit(context, request_id="input", text="synthetic")
    view_key, _, _ = keys(sessions, context)
    deadline = time.monotonic() + 1.2

    while time.monotonic() < deadline:
        sessions.record_activity(context)
        assert sessions.cache.client.exists(view_key)
        time.sleep(0.1)

    deadline = time.monotonic() + 1.3

    while time.monotonic() < deadline:
        sessions.read(context)
        sessions.publish(context, sessions.history.view(context))
        sessions.inspect(context)
        time.sleep(0.1)

    assert not sessions.cache.client.exists(view_key)
    assert sessions.read(context)["events"]
    assert not sessions.cache.client.exists(view_key)
    assert sessions.record_activity(context)
    assert sessions.read(context)["events"]
    assert sessions.cache.client.exists(view_key)


@pytest.mark.parametrize("finish", ["completed", "cancelled", "expired", "approval", "deleted"])
def test_inactive_work_stops_renewing_cache(
    sessions: SessionService, context: RunContext, dsn: str, finish: str
) -> None:
    runtime, handlers = fanout(sessions.store, context)
    lease = runtime.queue.claim("tool", handlers, seconds=10)
    assert lease is not None
    sessions.read(context)
    view_key, _, activity_key = keys(sessions, context)
    assert sessions.keep_active() == (1, None)

    if finish == "completed":
        state = runtime.queue.prepare(lease)
        runtime.queue.settle(lease, handlers.resolve(state).execute(context, state))
    elif finish == "cancelled":
        runtime.queue.cancel(context)
    elif finish == "expired":
        expire(dsn, lease)
    elif finish == "approval":
        runtime.queue.pause(lease, seconds=300, approval=True)
    else:
        sessions.store.delete_conversation(context)

    sessions.cache.client.pexpire(view_key, 200)
    sessions.cache.client.pexpire(activity_key, 200)
    assert sessions.keep_active() == (0, None)
    time.sleep(0.3)
    assert sessions.cache.client.exists(view_key, activity_key) == 0


def test_idle_cleanup_observes_worker_and_reader_activity(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(sessions.store, context)
    lease = runtime.queue.claim("tool", handlers, seconds=10)
    assert lease is not None
    sessions.project_due(ProjectionPolicy(events=1))
    age(dsn, context)
    view_key, _, activity_key = keys(sessions, context)
    cached = json.loads(cast(bytes, sessions.cache.client.get(view_key)))
    cached["last_access"] = 0
    sessions.cache.client.set(view_key, json.dumps(cached), ex=30)
    assert not sessions.cleanup(context, cached)
    expire(dsn, lease)

    # A reader racing a previously captured cleanup candidate updates only the
    # small marker. Lua must inspect that marker atomically before deleting.
    sessions.record_activity(context)
    assert not sessions.cleanup(context, cached)
    sessions.cache.client.set(activity_key, "0", ex=30)
    assert sessions.cleanup(context, cached)
    assert not sessions.cache.client.exists(view_key)


def test_retention_pagination_scope_and_recovery_fences(
    sessions: SessionService, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(sessions.store, context)
    assert runtime.queue.claim("tool", handlers, seconds=10) is not None
    other = replace(context, tenant_id="other-" + context.tenant_id)
    sessions.store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )
    fanout(sessions.store, other)
    assert runtime.queue.claim("tool", handlers, seconds=10) is not None
    assert runtime.queue.claim("tool", handlers, seconds=10) is not None
    assert runtime.queue.claim("tool", handlers, seconds=10) is not None

    # Per-conversation pagination deduplicates sibling leases and visits another
    # tenant's active conversation through the trusted maintenance role.
    first, cursor = sessions.keep_active(limit=1)
    assert first == 1 and cursor is not None
    second, cursor = sessions.keep_active(limit=1, after=cursor)
    assert second == 1 and cursor is not None
    assert sessions.keep_active(limit=1, after=cursor) == (0, None)
    sessions.read(context)
    view_key, _, _ = keys(sessions, context)
    sessions.cache.client.pexpire(view_key, 200)

    with pytest.raises(StateNotFoundError):
        sessions.progress_view(replace(context, user_id="someone-else"))

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id='new-timeline'")

    with pytest.raises(StateConflictError):
        sessions.keep_active()

    time.sleep(0.3)
    assert not sessions.cache.client.exists(view_key)


def test_redis_outage_does_not_change_durable_tool_state(
    sessions: SessionService, context: RunContext
) -> None:
    runtime, handlers = fanout(sessions.store, context)
    lease = runtime.queue.claim("tool", handlers, seconds=10)
    assert lease is not None
    before = sessions.store.load_run(context)
    unavailable = RedisSessions("redis://127.0.0.1:1/0")

    try:
        service = SessionService(sessions.store, unavailable)
        assert service.keep_active() == (1, None)
        assert sessions.store.load_run(context) == before
        runtime.queue.renew(lease)
    finally:
        unavailable.close()
