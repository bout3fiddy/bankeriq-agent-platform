"""Group 08 process-loss, compatibility and primary recovery acceptance gates."""

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from bankeriq_agent_platform.storage.history import ProjectionPolicy
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from bankeriq_agent_platform.workers.state import (
    Completion,
    Handler,
    Handlers,
    Recovery,
    replay_safe,
)

from .async_process import composition
from .test_async_groups import fanout, step
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import rows, wait_file


@pytest.mark.parametrize(
    "phase", ["admission", "claim", "model_response", "effect", "result_commit", "join", "final"]
)
def test_kill_each_application_boundary(
    store: PostgresStateStore, context: RunContext, dsn: str, tmp_path: Path, phase: str
) -> None:
    import asyncio

    runtime, handlers = composition(store)

    if phase != "admission":
        asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
        step(runtime, handlers, "model_prepare")

    if phase in ("effect", "result_commit", "join", "final"):
        step(runtime, handlers, "model_infer")

    if phase in ("join", "final"):
        for _ in range(2 if phase == "join" else 3):
            step(runtime, handlers, "tool")

    if phase == "final":
        step(runtime, handlers, "model_prepare")

    marker = tmp_path / phase
    command = [sys.executable, "-m", "tests.scaling.crash_process"]
    environment = {**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn}
    child = subprocess.Popen(
        command + ["crash", phase, context.tenant_id, str(marker)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_file(marker, child)
        child.kill()
        child.communicate(timeout=5)

        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second' "
                "WHERE status='leased'"
            )

        result = subprocess.run(
            command + ["resume", phase, context.tenant_id, str(marker)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert store.load_run(context).run.status == RunStatus.COMPLETED
        assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]
        assert rows(dsn, "SELECT count(*) FROM results") == [(3,)]
        assert rows(
            dsn,
            "SELECT count(*) FROM conversation_events WHERE "
            "payload->'value'->>'text'='Synthetic total saved'",
        ) == [(1,)]
        ledger = marker.with_suffix(".effects")
        assert not ledger.exists() or len(json.loads(ledger.read_text())) <= 3
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("flags", list(itertools.product((False, True), repeat=4)))
def test_every_saved_current_replay_combination(
    flags: tuple[bool, ...], store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    saved_idempotent, current_idempotent, saved_replay, current_replay = flags
    state = Recovery(
        kind="test",
        operation_id="operation",
        idempotent=saved_idempotent,
        replay_allowed=saved_replay,
    )
    handler = Handler(
        lambda c, s: True,
        lambda c, s: Completion(RunStatus.COMPLETED),
        idempotent=current_idempotent,
        replay_allowed=current_replay,
    )
    from agent_runtime.conversations.messages import Message, MessageRole
    from bankeriq_agent_platform.workers.queue import PostgresQueue

    from .test_workers import expire

    permitted = (saved_idempotent and current_idempotent) or (saved_replay and current_replay)
    assert replay_safe(state, handler) == permitted
    assert Recovery.decode(state.encode()) == state
    queue = PostgresQueue(store)
    queue.configure("test", concurrency=1, tenant_concurrency=1)
    queue.admit(
        context, request_id="request", items=(Message(MessageRole.USER, "input"),), state=state
    )
    entries = Handlers({("test", 1): handler})
    lease = queue.claim("test", entries)
    assert lease is not None
    queue.prepare(lease)
    expire(dsn, lease)
    recovered = PostgresQueue(PostgresStateStore(dsn)).claim("test", entries)
    assert (recovered is not None) == permitted
    assert rows(dsn, "SELECT status FROM jobs") == [("leased" if permitted else "reconcile",)]


def test_idle_cleanup_retains_recovery_and_deduplication(
    store: PostgresStateStore, context: RunContext, sessions: Any, dsn: str
) -> None:
    runtime, handlers = fanout(store, context)
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    runtime.queue.reconcile(lease)
    sessions.read(context)

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE conversations SET last_activity=now()-interval '1 hour'")

    # Materialize every committed cursor, then force only the disposable view's
    # idle timestamp old. Cleanup must not remove identities or unknown effects.
    for _ in range(100):
        sessions.project_due(limit=100)

        if not sessions.history.due(ProjectionPolicy()):
            break

    head = sessions.history.head(context)
    raw = json.loads(sessions.cache.client.get(sessions.cache.key(head)))
    raw["last_access"] = 0
    sessions.cache.client.set(sessions.cache.key(head), json.dumps(raw))
    sessions.cache.client.set(
        sessions.cache.key(head) + f":activity:{head['deletion_generation']}", "0", ex=1800
    )
    assert sessions.cleanup(context, raw)
    assert rows(dsn, "SELECT count(*) FROM admissions") == [(1,)]
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE effect_pending") == [(1,)]
    assert store.load_run(context).run.status == RunStatus.WAITING
    before = rows(dsn, "SELECT count(*) FROM jobs")
    runtime.queue.admit(
        context,
        request_id="request",
        items=store.load_run(context).conversation.items[:1],
        state=runtime.initial(context),
    )
    assert rows(dsn, "SELECT count(*) FROM jobs") == before


def test_primary_routing_rejects_read_only_session(dsn: str) -> None:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    settings = conninfo_to_dict(dsn)
    options = str(settings.get("options", "")) + " -c default_transaction_read_only=on"

    from agent_runtime.prompts.resolution import PromptScope
    from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver

    read_only = make_conninfo(dsn, options=options)

    with pytest.raises(psycopg.OperationalError, match="read-only"):
        PostgresStateStore(read_only)

    with pytest.raises(psycopg.OperationalError, match="read-only"):
        PostgresPromptResolver(read_only, PromptScope("bankeriq", "test"))


def test_migration_failure_rolls_back_and_old_reader_still_works(
    store: PostgresStateStore,
    context: RunContext,
    dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from bankeriq_agent_platform.storage import postgres

    runtime, _ = composition(store)
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    baseline = rows(dsn, "SELECT * FROM schema_versions ORDER BY version")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "999-invalid.sql").write_text(
        "ALTER TABLE jobs ADD COLUMN must_rollback boolean; SELECT missing_column FROM jobs;"
    )

    class MigrationPath:
        def __init__(self, source):
            pass

        def with_name(self, name):
            return migrations

    with monkeypatch.context() as patch:
        patch.setattr(postgres, "Path", MigrationPath)

        with pytest.raises(psycopg.errors.UndefinedColumn):
            postgres.migrate(dsn)

    assert rows(dsn, "SELECT * FROM schema_versions ORDER BY version") == baseline
    assert not rows(
        dsn,
        "SELECT 1 FROM information_schema.columns WHERE "
        "table_schema=current_schema() AND column_name='must_rollback'",
    )
    assert store.load_run(context).run.status == RunStatus.QUEUED
    postgres.migrate(dsn)


def test_scaling_signal_excludes_sleepers_and_approval_waits(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    from bankeriq_agent_platform.workers.scaling import pressure

    runtime, handlers = fanout(store, context)
    assert pressure(runtime.queue, "tool", handlers).eligible == 3
    first = runtime.queue.claim("tool", handlers)
    second = runtime.queue.claim("tool", handlers)
    assert first is not None and second is not None
    runtime.queue.pause(first, seconds=60)
    runtime.queue.pause(second, seconds=60, approval=True)
    view = pressure(runtime.queue, "tool", handlers)
    assert view.eligible == 1 and view.leased == 0 and view.concurrency == 4
    assert pressure(runtime.queue, "tool", Handlers({})).eligible == 0


def test_progress_never_reconstructs_history(
    store: PostgresStateStore, context: RunContext, sessions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bankeriq_agent_platform.sessions.stream import ProgressStream

    fanout(store, context)

    def forbidden(*args, **kwargs):
        raise AssertionError("progress attempted transcript reconstruction")

    monkeypatch.setattr(store, "_recent_events", forbidden)
    monkeypatch.setattr(sessions, "read", forbidden)
    sessions.progress(context, sequence=1, status="running")
    stream = ProgressStream(sessions, context)
    stream.poll()
    assert stream.buffer.size > 0


def test_connection_pool_bounds_waits_and_rolls_back(dsn: str) -> None:
    from psycopg_pool import PoolTimeout

    pooled = PostgresStateStore(dsn, pool_size=1)

    try:
        with pooled._connect() as first:
            pid = first.execute("SELECT pg_backend_pid() AS pid").fetchone()

            with pytest.raises(PoolTimeout), pooled._connect():
                raise AssertionError("pool exceeded its configured maximum")

        with pytest.raises(RuntimeError), pooled._connect() as connection:
            connection.execute("CREATE TABLE must_rollback (n integer)")
            raise RuntimeError("rollback")

        with pooled._connect() as connection:
            assert connection.execute("SELECT pg_backend_pid() AS pid").fetchone() == pid
            row = connection.execute("SELECT to_regclass('must_rollback') AS name").fetchone()
            assert row == {"name": None}

        assert pooled._connections.pool is not None
        assert pooled._connections.pool.get_stats()["pool_size"] == 1
    finally:
        pooled.close()


def test_mixed_compatible_worker_processes(
    store: PostgresStateStore, dsn: str, tmp_path: Path
) -> None:
    from agent_runtime.conversations.messages import Message, MessageRole
    from bankeriq_agent_platform.workers.queue import PostgresQueue

    queue = PostgresQueue(store)
    queue.configure("versioned", concurrency=4, tenant_concurrency=4)

    for version in (1, 2, 3):
        for index in range(3):
            context = RunContext(
                "mixed", "user", f"chat-{version}-{index}", f"run-{version}-{index}"
            )
            store.create_conversation(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                conversation_id=context.conversation_id,
            )
            queue.admit(
                context,
                request_id=context.run_id,
                items=(Message(MessageRole.USER, "input"),),
                state=Recovery(
                    kind="versioned", version=version, operation_id=context.run_id, idempotent=True
                ),
            )

    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.scaling.version_process",
                str(version),
                str(tmp_path / str(version)),
            ],
            env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for version in (1, 2)
    ]

    try:
        for child in children:
            _, error = child.communicate(timeout=20)
            assert child.returncode == 0, error

        assert rows(dsn, "SELECT count(*) FROM runs WHERE status='completed'") == [(6,)]
        assert rows(dsn, "SELECT status FROM jobs WHERE operation_version=3") == [("ready",)] * 3
        assert [(tmp_path / str(v)).read_text() for v in (1, 2)] == ["6", "6"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
