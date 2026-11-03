"""Real-primary worker recovery, fencing and independent-process acceptance gates."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any

import psycopg
import pytest

from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunStatus
from agent_harness.storage.codec import plain
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.queue import Lease, PostgresQueue
from agent_runtime.workers.runner import Listener, Worker
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery

from .test_postgres import INPUT, Fault
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .worker_process import complete


@pytest.fixture
def queue(store: PostgresStateStore) -> PostgresQueue:
    queue = PostgresQueue(store)
    queue.configure("synthetic", concurrency=4, tenant_concurrency=2)
    return queue


@pytest.fixture
def state() -> Recovery:
    return Recovery(kind="synthetic", operation_id="operation-1", idempotent=True)


@pytest.fixture
def handlers() -> Handlers:
    return Handlers({("synthetic", 1): Handler(lambda c, s: True, complete, idempotent=True)})


def admit(queue: PostgresQueue, context: RunContext, state: Recovery) -> None:
    queue.admit(context, request_id=context.run_id, items=INPUT, state=state)


def claim(queue: PostgresQueue, handlers: Handlers, seconds: float = 5) -> Lease:
    lease = queue.claim("synthetic", handlers, seconds=seconds)
    assert lease is not None
    return lease


def expire(dsn: str, lease: Lease) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND job_id=%s",
            (lease.context.tenant_id, lease.job_id),
        )


def rows(dsn: str, query: str) -> list[Any]:
    with psycopg.connect(dsn) as connection:
        return connection.execute(query.encode()).fetchall()


def process(dsn: str, *args: str, stdin: bool = False) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.integration.worker_process", *args],
        env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
        stdin=subprocess.PIPE if stdin else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_file(path: Path, child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10

    while not path.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert path.exists(), child.communicate(timeout=1)


def test_worker_loss_duplicate_notify_one_result_and_continuation(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
    tmp_path: Path,
    sessions: Any,
) -> None:
    admit(queue, context, state)
    marker = tmp_path / "dispatch"
    first = process(dsn, "die", "synthetic", str(marker))

    try:
        wait_file(marker, first)
        first.kill()
        first.communicate(timeout=5)
        assert first.returncode != 0

        # There is no listener when these duplicate notifications commit. The
        # second process must find the expired durable lease by polling alone.
        with psycopg.connect(dsn) as connection:
            connection.execute("SELECT pg_notify('runtime_jobs','coordinator:run:0')")
            connection.execute("SELECT pg_notify('runtime_jobs','coordinator:run:0')")

        second = process(dsn, "run", "synthetic")
        output, error = second.communicate(timeout=10)
        assert second.returncode == 0, error
        assert "settled" in output
        assert rows(dsn, "SELECT status,ownership_token FROM jobs ORDER BY input_revision") == [
            ("completed", 2),
            ("ready", 0),
        ]
        assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]
        assert rows(
            dsn,
            "SELECT count(*) FROM conversation_events WHERE "
            "payload->'value'->>'text'='synthetic saved result'",
        ) == [(1,)]

        # Projection is repairable after worker loss and Redis loss. No handler
        # executes during reconstruction of this accepted/saved UI state.
        sessions.cache.client.flushdb()
        view = sessions.read(context)
        assert "synthetic saved result" in json.dumps(view)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)


def test_stale_renewal_and_settlement_and_unknown_commit(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    first = claim(queue, handlers)
    queue.prepare(first)
    expire(dsn, first)
    second = claim(queue, handlers)
    assert second.token == first.token + 1

    for action in (
        lambda: queue.renew(first),
        lambda: queue.settle(first, complete(context, state)),
    ):
        with pytest.raises(StateConflictError):
            action()

    faulty = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    saved = faulty.settle(second, complete(context, state))
    assert saved.run.revision == 1
    assert queue.settle(second, complete(context, state)) == saved
    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]

    with pytest.raises(StateConflictError, match="conflicting"):
        queue.settle(second, Completion(RunStatus.RUNNING))


def test_two_process_settlement_race(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
    tmp_path: Path,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers, seconds=20)
    release = tmp_path / "release"
    children = []
    payload = json.dumps(
        {
            "context": plain(context),
            "job_id": lease.job_id,
            "owner": lease.owner,
            "token": lease.token,
            "state": state.encode(),
            "attempt": lease.attempt,
        }
    )

    try:
        for index in range(2):
            marker = tmp_path / str(index)
            child = process(dsn, "settle", "synthetic", str(marker), str(release), stdin=True)
            children.append(child)
            assert child.stdin is not None
            child.stdin.write(payload)
            child.stdin.close()
            child.stdin = None
            wait_file(marker, child)

        release.write_text("go")

        for child in children:
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            assert output.strip() == "1"

        assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]
        assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='ready'") == [(1,)]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_retry_wait_cancel_and_access_recheck(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    first = claim(queue, handlers)
    queue.pause(first, seconds=0)
    second = claim(queue, handlers)
    assert second.attempt == 2
    queue.pause(second, seconds=60, approval=True)
    assert queue.claim("synthetic", handlers) is None

    with pytest.raises(StateConflictError):
        queue.approve(context, second.job_id, state.model_copy(update={"arguments": {"a": 3}}))

    queue.approve(context, second.job_id, state)
    third = claim(queue, handlers)
    calls = []
    denied = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: False, lambda c, s: calls.append(s) or complete(c, s)
            )
        }
    )
    Worker(queue, denied, queue_name="synthetic").execute(third)
    assert calls == []
    assert queue.store.load_run(context).run.status == RunStatus.FAILED


def test_cancellation_fences_active_owner(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    cancelled = queue.cancel(context)
    assert cancelled.run.status == RunStatus.CANCELLED
    assert queue.cancel(context) == cancelled

    with pytest.raises(StateConflictError):
        queue.settle(lease, complete(context, state))

    assert queue.claim("synthetic", handlers) is None


@pytest.mark.parametrize(
    "phase", ["ready", "dispatch", "retry", "approval", "reconcile", "complete"]
)
def test_each_recovery_phase_in_fresh_process(phase: str) -> None:
    state = Recovery.decode(
        {**Recovery(kind="synthetic", operation_id="op").encode(), "phase": phase}
    )
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.worker_process", "codec"],
        input=json.dumps(state.encode()),
        capture_output=True,
        text=True,
        check=True,
    )
    assert Recovery.decode(json.loads(result.stdout)) == state


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), b"secret", {1: "key"}, object()])
def test_invalid_json_has_no_partial_admission(
    queue: PostgresQueue,
    context: RunContext,
    invalid: Any,
    dsn: str,
) -> None:
    with pytest.raises((ValueError, TypeError)):
        admit(
            queue, context, Recovery(kind="synthetic", operation_id="op", arguments={"x": invalid})
        )

    assert rows(dsn, "SELECT count(*) FROM admissions") == [(0,)]
    assert rows(dsn, "SELECT count(*) FROM jobs") == [(0,)]


def test_incompatible_and_conflicting_identity(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    admit(queue, context, state)

    with pytest.raises(StateConflictError):
        admit(queue, context, state.model_copy(update={"arguments": {"changed": True}}))

    assert queue.claim("synthetic", Handlers({})) is None
    assert queue.claim("synthetic", Handlers({("synthetic", 2): handlers.resolve(state)})) is None
    assert rows(dsn, "SELECT attempt FROM jobs") == [(0,)]

    with pytest.raises(StateNotFoundError):
        queue.attach(replace(context, user_id="another"), state)

    assert claim(queue, handlers).attempt == 1


def test_unsafe_unknown_effect_requires_lookup(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    unsafe = state.model_copy(update={"idempotent": False})
    admit(queue, context, unsafe)
    executions: list[str] = []
    lookups: list[str] = []

    def execute(context: RunContext, state: Recovery) -> Completion:
        executions.append(state.operation_id)
        raise RuntimeError("effect succeeded but response lost")

    def lookup(context: RunContext, state: Recovery) -> Completion:
        lookups.append(state.operation_id)
        return complete(context, state)

    handlers = Handlers({("synthetic", 1): Handler(lambda c, s: True, execute, reconcile=lookup)})
    worker = Worker(queue, handlers, queue_name="synthetic")
    worker.execute(claim(queue, handlers))
    assert rows(dsn, "SELECT status FROM jobs") == [("reconcile",)]

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE jobs SET due_at=clock_timestamp() WHERE status='reconcile'")

    worker.execute(claim(queue, handlers))
    assert executions == ["operation-1"]
    assert lookups == ["operation-1"]
    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]


def test_renewal_settlement_race(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    barrier = Barrier(2)

    def renew() -> None:
        barrier.wait()

        with suppress(StateConflictError):
            queue.renew(lease)

    def settle() -> None:
        barrier.wait()
        queue.settle(lease, complete(context, state))

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(renew), executor.submit(settle)]

        for future in futures:
            future.result()

    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]


def test_heartbeat_and_graceful_drain(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    admit(queue, context, state)
    entered = Event()
    release = Event()
    stop = Event()

    def slow(context: RunContext, state: Recovery) -> Completion:
        entered.set()
        assert release.wait(5)
        return complete(context, state)

    handlers = Handlers({("synthetic", 1): Handler(lambda c, s: True, slow, idempotent=True)})
    worker = Worker(
        queue, handlers, queue_name="synthetic", concurrency=1, lease_seconds=0.3, poll_seconds=0.02
    )
    thread = Thread(target=worker.run, args=(stop,))
    thread.start()

    try:
        assert entered.wait(5)
        stop.set()
        time.sleep(0.5)
        assert thread.is_alive()
        assert queue.claim("synthetic", handlers) is None
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]
        assert rows(dsn, "SELECT status FROM jobs WHERE input_revision=1") == [("ready",)]
    finally:
        release.set()
        stop.set()
        thread.join(5)


def test_listener_reconnect_and_poll_timeout(dsn: str) -> None:
    listener = Listener(dsn)
    listener.wait(0.01)
    assert listener.connection is not None
    listener.connection.close()
    listener.wait(0.01)
    assert listener.connection is None
    listener.wait(0.01)
    assert listener.connection is not None
    listener.close()


def test_capacity_across_processes_and_tenants(
    queue: PostgresQueue,
    state: Recovery,
    dsn: str,
) -> None:
    for tenant in ("tenant-a", "tenant-b"):
        for index in range(3):
            context = RunContext(tenant, "user", f"chat-{index}", f"run-{index}")
            queue.store.create_conversation(
                tenant_id=tenant, user_id="user", conversation_id=context.conversation_id
            )
            admit(queue, context, state.model_copy(update={"operation_id": f"operation-{index}"}))

    children = [process(dsn, "claim", "synthetic") for _ in range(6)]
    claimed = []

    for child in children:
        output, error = child.communicate(timeout=10)
        assert child.returncode == 0, error
        result = json.loads(output)

        if result:
            claimed.append(result)

    assert len(claimed) == 4
    assert sum(row["tenant_id"] == "tenant-a" for row in claimed) == 2
    assert sum(row["tenant_id"] == "tenant-b" for row in claimed) == 2


def test_skip_locked_conversation_preserves_independent_progress(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    other = replace(context, conversation_id="other", run_id="other")
    queue.store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )
    admit(queue, other, state.model_copy(update={"operation_id": "other"}))

    with psycopg.connect(dsn) as connection:
        connection.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=%s FOR UPDATE",
            (context.conversation_id,),
        )
        lease = claim(queue, handlers)
        assert lease.context == other


@pytest.mark.parametrize(
    "boundary", ["worker_after_output", "worker_after_next", "worker_after_settlement"]
)
def test_atomic_settlement_rollback(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
    boundary: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)

    def fail(phase: str) -> None:
        if phase == boundary:
            raise RuntimeError("synthetic boundary failure")

    faulty = PostgresQueue(PostgresStateStore(dsn, fault=fail))

    with pytest.raises(RuntimeError, match="boundary"):
        faulty.settle(lease, complete(context, state))

    assert rows(dsn, "SELECT count(*) FROM transitions") == [(0,)]
    assert rows(dsn, "SELECT status FROM jobs") == [("leased",)]
    assert rows(dsn, "SELECT revision FROM runs") == [(0,)]
    assert queue.settle(lease, complete(context, state)).run.revision == 1


def test_expiry_during_settlement_rolls_everything_back(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers, seconds=0.2)

    def delay(phase: str) -> None:
        if phase == "worker_after_next":
            time.sleep(0.25)

    delayed = PostgresQueue(PostgresStateStore(dsn, fault=delay))

    with pytest.raises(StateConflictError, match="expired"):
        delayed.settle(lease, complete(context, state))

    assert rows(dsn, "SELECT count(*) FROM transitions") == [(0,)]
    recovered = claim(queue, handlers)
    assert recovered.token == 2


def test_attempt_and_run_budgets_and_approval_expiry(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    limited = state.model_copy(update={"attempt_limit": 1})
    admit(queue, context, limited)
    lease = claim(queue, handlers)
    queue.pause(lease, seconds=0)
    assert queue.claim("synthetic", handlers) is None
    assert rows(dsn, "SELECT status FROM jobs") == [("exhausted",)]
    assert queue.store.load_run(context).run.status == RunStatus.FAILED

    next_context = replace(context, run_id="budget")
    limited = state.model_copy(update={"operation_id": "budget", "run_budget": 1})
    admit(queue, next_context, limited)
    lease = claim(queue, handlers)
    queue.settle(lease, complete(next_context, limited))
    assert queue.claim("synthetic", handlers) is None
    assert queue.store.load_run(next_context).run.status == RunStatus.LIMIT_REACHED

    approval_context = replace(context, run_id="approval")
    waiting = state.model_copy(update={"operation_id": "approval"})
    admit(queue, approval_context, waiting)
    lease = claim(queue, handlers)
    queue.pause(lease, seconds=0, approval=True)

    with pytest.raises(StateConflictError, match="expired"):
        queue.approve(approval_context, lease.job_id, waiting)

    assert queue.claim("synthetic", handlers) is None
    assert queue.store.load_run(approval_context).run.status == RunStatus.FAILED
    assert ("approval_expired",) in rows(dsn, "SELECT status FROM jobs")


def test_control_unknown_commits_do_not_consume_wait_twice(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    faulty = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    lease = claim(faulty, handlers)
    assert lease.attempt == 1
    assert rows(dsn, "SELECT ownership_token FROM jobs") == [(1,)]
    faulty = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    faulty.pause(lease, seconds=60, approval=True)
    faulty = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    faulty.approve(context, lease.job_id, state)
    assert claim(queue, handlers).attempt == 2


def test_database_retry_never_reexecutes_handler(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    calls = []

    def execute(context: RunContext, state: Recovery) -> Completion:
        calls.append(state.operation_id)
        return complete(context, state)

    fault = Fault(lost_ack=True)

    def fail_settlement(phase: str) -> None:
        if calls:
            fault(phase)

    worker = Worker(
        PostgresQueue(PostgresStateStore(dsn, fault=fail_settlement)),
        Handlers({("synthetic", 1): Handler(lambda c, s: True, execute)}),
        queue_name="synthetic",
    )
    worker.execute(lease)
    assert calls == ["operation-1"]
    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]


def test_recovery_schema_rejects_unknown_version_fields_and_invalid_values() -> None:
    value = Recovery(kind="synthetic", operation_id="operation").encode()

    for changes in (
        {"format_version": 2},
        {"client": "handle"},
        {"phase": "invented"},
        {"version": True},
        {"run_budget": 0},
        {"arguments": []},
    ):
        with pytest.raises(ValueError):
            Recovery.decode({**value, **changes})


def test_connection_reserve_is_validated(queue: PostgresQueue, handlers: Handlers) -> None:
    with pytest.raises(ValueError, match="reserved connection"):
        Worker(
            queue,
            handlers,
            queue_name="synthetic",
            concurrency=4,
            connection_budget=10,
            api_reserve=2,
            process_limit=2,
        )


def test_restricted_output_and_delivery_remain_isolated(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    from agent_harness.policy.output import enforce_output
    from agent_harness.tools.definitions import OutputPolicy, ToolCall, ToolDefinition

    private = "SYNTHETIC_PRIVATE_REPORT_BODY"
    definition = ToolDefinition("report", "1", "synthetic report", {}, OutputPolicy.RESTRICTED)
    admit(queue, context, state)

    def report(context: RunContext, state: Recovery) -> Completion:
        if not state.arguments:
            return Completion(
                RunStatus.RUNNING,
                (ToolCall("call-1", "report", {}),),
                next_task=state.model_copy(
                    update={
                        "phase": "ready",
                        "operation_id": "report",
                        "arguments": {"dispatch": True},
                    }
                ),
            )

        enforced = enforce_output(
            definition, "call-1", {"private": private}, artifact_id="report-9"
        )
        assert enforced.artifact is not None
        return Completion(
            RunStatus.COMPLETED, (enforced.model_result,), artifacts=(enforced.artifact,)
        )

    handlers = Handlers({("synthetic", 1): Handler(lambda c, s: True, report, idempotent=True)})
    worker = Worker(queue, handlers, queue_name="synthetic")
    worker.execute(claim(queue, handlers))
    worker.execute(claim(queue, handlers))
    checkpoint = queue.store.load_run(context)
    assert private not in str(plain(checkpoint))
    assert "report-9" not in str(plain(checkpoint.conversation))
    assert queue.store.deliveries(context)[0].artifact_id == "report-9"

    for table in ("jobs", "runs", "conversation_events", "results", "transitions"):
        assert private not in str(rows(dsn, f"SELECT row_to_json(t) FROM {table} t"))


def test_changed_replay_policy_and_cancelled_unknown_effect(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    saved = state.model_copy(update={"idempotent": False, "replay_allowed": True})
    admit(queue, context, saved)
    allowed = Handlers(
        {("synthetic", 1): Handler(lambda c, s: True, complete, replay_allowed=True)}
    )
    lease = claim(queue, allowed)
    queue.prepare(lease)
    expire(dsn, lease)
    revoked = Handlers({("synthetic", 1): Handler(lambda c, s: True, complete)})
    assert queue.claim("synthetic", revoked) is None
    assert rows(dsn, "SELECT status FROM jobs") == [("reconcile",)]
    queue.cancel(context)
    assert rows(dsn, "SELECT status FROM jobs") == [("cancelled_unknown",)]
    assert queue.claim("synthetic", allowed) is None


def test_recovered_lease_checks_timeline_and_deletion(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id='new-timeline'")

    fresh = PostgresQueue(PostgresStateStore(dsn))

    with pytest.raises(StateConflictError, match="timeline"):
        fresh.renew(lease)

    with pytest.raises(StateConflictError):
        fresh.settle(lease, complete(context, state))

    fresh.store.delete_conversation(context)

    with pytest.raises(StateNotFoundError):
        fresh.prepare(lease)

    assert fresh.claim("synthetic", handlers) is None


@pytest.mark.parametrize(
    "phase", ["ready", "dispatch", "retry", "approval", "reconcile", "complete"]
)
def test_saved_phase_reconstructs_from_primary_in_fresh_process(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
    phase: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)

    match phase:
        case "dispatch":
            queue.prepare(lease)
        case "retry":
            queue.pause(lease, seconds=60)
        case "approval":
            queue.pause(lease, seconds=60, approval=True)
        case "reconcile":
            queue.prepare(lease)
            queue.reconcile(lease)
        case "complete":
            queue.settle(lease, complete(context, state))

    child = process(dsn, "restore", "synthetic", stdin=True)
    output, error = child.communicate(
        json.dumps({"context": plain(context), "job_id": lease.job_id}),
        timeout=10,
    )
    assert child.returncode == 0, error
    restored = Recovery.decode(json.loads(output))
    assert restored.phase == phase
    assert restored.operation_id == state.operation_id
    assert restored.arguments == state.arguments


def test_process_dies_after_unsafe_effect_and_fresh_process_reconciles(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
    tmp_path: Path,
) -> None:
    unsafe = state.model_copy(update={"idempotent": False})
    admit(queue, context, unsafe)
    provider_record = tmp_path / "provider-record"
    first = process(dsn, "effect-die", "synthetic", str(provider_record))

    try:
        wait_file(provider_record, first)
        first.kill()
        first.communicate(timeout=5)
        second = process(dsn, "reconcile-run", "synthetic", str(provider_record))
        _, error = second.communicate(timeout=10)
        assert second.returncode == 0, error
        assert provider_record.read_text() == "operation-1"
        assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]
        assert rows(dsn, "SELECT status FROM jobs ORDER BY input_revision") == [
            ("completed",),
            ("ready",),
        ]
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)


def test_notify_payload_is_id_only(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    listener = Listener(dsn)

    try:
        listener.wait(0.01)
        admit(queue, context, state)
        assert listener.connection is not None
        notifications = list(listener.connection.notifies(timeout=1, stop_after=1))
        assert len(notifications) == 1
        assert notifications[0].payload == f"coordinator:{context.run_id}:0"
    finally:
        listener.close()


def test_bounded_worker_maintenance_repairs_projection(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    sessions: Any,
    dsn: str,
) -> None:
    from agent_runtime.storage.history import ProjectionPolicy

    admit(queue, context, state)
    stop = Event()

    def maintain() -> None:
        sessions.project_due(ProjectionPolicy(events=1), limit=1)
        stop.set()

    Worker(
        queue,
        handlers,
        queue_name="synthetic",
        concurrency=1,
        maintenance=maintain,
        poll_seconds=0.01,
    ).run(stop)
    assert rows(dsn, "SELECT dirty_cursor=cache_cursor FROM projection_work") == [(True,)]
    assert "synthetic input" in json.dumps(sessions.read(context))


def test_conflicting_operation_identity_rolls_back_new_admission(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    dsn: str,
) -> None:
    admit(queue, context, state)
    other = replace(context, run_id="other")

    with pytest.raises(StateConflictError):
        admit(queue, other, state)

    assert rows(dsn, "SELECT count(*) FROM admissions") == [(1,)]
    assert rows(dsn, "SELECT count(*) FROM runs") == [(1,)]


def test_nonterminal_settlement_requires_recoverable_next_work(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)

    with pytest.raises(ValueError, match="next work"):
        queue.settle(lease, Completion(RunStatus.RUNNING))

    assert rows(dsn, "SELECT count(*) FROM transitions") == [(0,)]
    assert rows(dsn, "SELECT status FROM jobs") == [("leased",)]


def test_reconciliation_unknown_commit_is_idempotent(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    queue.prepare(lease)
    faulty = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    faulty.reconcile(lease)
    assert rows(dsn, "SELECT status,ownership_token FROM jobs") == [("reconcile", 1)]
