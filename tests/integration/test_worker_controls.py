"""Local-service acceptance for worker inspection, crash limits and operator recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import psycopg
import pytest

from agent_harness.runs.context import RunContext
from agent_harness.storage.codec import plain
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.controls import (
    ControlRequest,
    RecoveryActor,
    RecoveryControls,
)
from agent_runtime.workers.queue import Lease, PostgresQueue
from agent_runtime.workers.reasons import FailureReason
from agent_runtime.workers.runner import Worker
from agent_runtime.workers.state import Handler, Handlers, Recovery

from .test_postgres import INPUT, Fault
from .test_workers import admit, claim, expire, process, rows, wait_file
from .test_workers import context as context
from .test_workers import dsn as dsn
from .test_workers import handlers as handlers
from .test_workers import primary_dsn as primary_dsn
from .test_workers import queue as queue
from .test_workers import redis_url as redis_url
from .test_workers import state as state
from .test_workers import store as store
from .worker_process import complete


@pytest.fixture
def actor(context: RunContext) -> RecoveryActor:
    return RecoveryActor(context.tenant_id, "operator-7")


@pytest.fixture
def controls(queue: PostgresQueue, handlers: Handlers) -> RecoveryControls:
    return RecoveryControls(queue, handlers, authorize=lambda a, c, action: True)


def command(view: Any, request_id: str = "control-1", action: str = "retry") -> ControlRequest:
    return ControlRequest.model_validate(
        dict(
            request_id=request_id,
            action=action,
            expected_revision=view.revision,
            recovery_id=view.recovery_id,
        )
    )


def test_inspection_excludes_arguments_and_checks_live_access(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
) -> None:
    state = state.model_copy(update={"arguments": {"private": "PRIVATE_MARKER"}})
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    view = controls.inspect(actor, context, job_id)
    assert view.operation_id == state.operation_id
    assert view.reason == FailureReason.NONE
    assert "retry" in view.allowed_actions
    assert "PRIVATE_MARKER" not in json.dumps(plain(view))
    allowed = True
    restricted = RecoveryControls(queue, handlers, authorize=lambda a, c, action: allowed)
    assert restricted.inspect(actor, context, job_id) == view
    allowed = False

    with pytest.raises(PermissionError):
        restricted.inspect(actor, context, job_id)

    with pytest.raises(PermissionError):
        controls.inspect(replace(actor, tenant_id="another-tenant"), context, job_id)

    with pytest.raises(StateNotFoundError):
        controls.inspect(actor, replace(context, user_id="another-user"), job_id)


def test_controls_are_audited_duplicate_safe_and_reject_conflicts(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    queue.pause(lease, seconds=60)
    view = controls.inspect(actor, context, lease.job_id)
    request = command(view)
    receipt = controls.apply(actor, context, lease.job_id, request)
    assert receipt.decision == "accepted"
    assert receipt.revision > view.revision
    assert controls.apply(actor, context, lease.job_id, request) == receipt
    audit = controls.decisions(actor, context, lease.job_id)
    assert audit == (
        {
            "request_id": "control-1",
            "actor_id": "operator-7",
            "action": "retry",
            "decision": "accepted",
        },
    )
    assert rows(dsn, "SELECT attempt FROM jobs") == [(1,)]
    assert rows(dsn, "SELECT worker_attempts,worker_budget FROM runs") == [(1, 16)]

    with pytest.raises(StateConflictError, match="conflicting"):
        controls.apply(
            actor, context, lease.job_id, request.model_copy(update={"action": "reconcile"})
        )

    assert (
        controls.apply(
            actor, context, lease.job_id, request.model_copy(update={"request_id": "stale"})
        ).decision
        == "stale"
    )
    recovered = claim(queue, handlers)
    assert recovered.state.operation_id == state.operation_id
    assert recovered.attempt == 2


def test_unknown_commit_and_rollback_of_control_and_audit(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    def rollback(phase: str) -> None:
        if phase == "after_control_write":
            raise RuntimeError("synthetic transaction failure")

    failing = RecoveryControls(
        PostgresQueue(PostgresStateStore(dsn, fault=rollback)),
        handlers,
        authorize=lambda a, c, action: True,
    )

    with pytest.raises(RuntimeError):
        failing.apply(actor, context, job_id, request)

    assert rows(dsn, "SELECT count(*) FROM worker_controls") == [(0,)]
    assert controls.inspect(actor, context, job_id).revision == request.expected_revision
    lost = RecoveryControls(
        PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True))),
        handlers,
        authorize=lambda a, c, action: True,
    )
    assert lost.apply(actor, context, job_id, request).decision == "accepted"
    assert rows(dsn, "SELECT count(*) FROM worker_controls") == [(1,)]


def test_denied_requests_record_safe_decisions_and_access_is_rechecked(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    def error(*args: Any) -> bool:
        raise RuntimeError("SECRET_CREDENTIAL_AND_PRIVATE_BODY")

    denied = RecoveryControls(queue, handlers, authorize=error)
    receipt = denied.apply(actor, context, job_id, request)
    assert receipt.decision == "access_denied"
    assert receipt.revision == 0
    assert "SECRET" not in str(rows(dsn, "SELECT row_to_json(c) FROM worker_controls c"))
    assert controls.apply(actor, context, job_id, request) == receipt
    assert (
        controls.apply(
            actor, context, job_id, request.model_copy(update={"request_id": "new"})
        ).decision
        == "accepted"
    )
    assert (
        denied.apply(
            actor, context, job_id, request.model_copy(update={"request_id": "new"})
        ).decision
        == "access_denied"
    )


def test_simultaneous_controls_have_one_winner(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    view = controls.inspect(actor, context, job_id)

    with ThreadPoolExecutor(2) as executor:
        futures = [
            executor.submit(
                controls.apply, actor, context, job_id, command(view, f"request-{index}")
            )
            for index in range(2)
        ]
        decisions = [future.result().decision for future in futures]

    assert sorted(decisions) == ["accepted", "stale"]
    assert rows(dsn, "SELECT count(*) FROM worker_controls") == [(2,)]


def test_approval_revocation_and_expiry_prevent_operator_retry_and_dispatch(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    queue.pause(lease, seconds=60, approval=True)
    queue.approve(context, lease.job_id, state)
    allowed = True
    calls: list[str] = []

    def execute(c: RunContext, s: Recovery):
        calls.append(s.operation_id)
        return complete(c, s)

    current = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: True, execute, idempotent=True, approval_valid=lambda c, s: allowed
            )
        }
    )
    recovery = RecoveryControls(queue, current, authorize=lambda a, c, action: True)
    request = command(recovery.inspect(actor, context, lease.job_id))
    allowed = False
    assert recovery.apply(actor, context, lease.job_id, request).decision == "approval_denied"
    allowed = True
    second = claim(queue, current)
    queue.prepare(second)
    queue.pause(second, seconds=60, handler=current.resolve(state))

    # Retry scheduling must preserve the original expiry and approval binding.
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE jobs SET wait_expiry=clock_timestamp()-interval '1 second'")

    request = command(recovery.inspect(actor, context, lease.job_id), "expired")
    assert recovery.apply(actor, context, lease.job_id, request).decision == "approval_denied"
    assert rows(dsn, "SELECT approval_fingerprint IS NOT NULL FROM jobs") == [(True,)]
    assert calls == []


def test_unknown_effect_cannot_bypass_reconciliation_or_current_replay_policy(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    dsn: str,
) -> None:
    saved = state.model_copy(update={"idempotent": False, "replay_allowed": True})
    admit(queue, context, saved)
    permitted_handlers = Handlers(
        {("synthetic", 1): Handler(lambda c, s: True, complete, replay_allowed=True)}
    )
    lease = claim(queue, permitted_handlers)
    queue.prepare(lease)
    queue.pause(lease, seconds=60, handler=permitted_handlers.resolve(saved))
    revoked = Handlers({("synthetic", 1): Handler(lambda c, s: True, complete)})
    controls = RecoveryControls(queue, revoked, authorize=lambda a, c, action: True)
    view = controls.inspect(actor, context, lease.job_id)
    assert view.unknown_effect
    assert controls.apply(actor, context, lease.job_id, command(view)).decision == "replay_denied"

    # Advancing the deadline cannot turn a saved safe-retry phase into permission
    # to replay after the current policy has been revoked.
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE jobs SET due_at=clock_timestamp()")

    assert queue.claim("synthetic", revoked) is None
    lookups: list[str] = []

    def lookup(c: RunContext, s: Recovery):
        lookups.append(s.operation_id)
        return complete(c, s)

    reconciler = Handlers(
        {("synthetic", 1): Handler(lambda c, s: True, complete, reconcile=lookup)}
    )
    controls = RecoveryControls(queue, reconciler, authorize=lambda a, c, action: True)
    view = controls.inspect(actor, context, lease.job_id)
    assert view.allowed_actions == ("reconcile",)
    request = command(view, "lookup", "reconcile")
    receipt = controls.apply(actor, context, lease.job_id, request)
    assert receipt.decision == "accepted"
    Worker(queue, reconciler, queue_name="synthetic").execute(claim(queue, reconciler))
    assert lookups == [saved.operation_id]
    assert controls.apply(actor, context, lease.job_id, request) == receipt
    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]


@pytest.mark.parametrize("mode", ["die", "pre-die"])
def test_repeated_process_death_stops_at_durable_allowance_and_other_tenant_progresses(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
    tmp_path: Path,
    mode: str,
) -> None:
    admit(queue, context, state)

    for index in range(state.attempt_limit):
        marker = tmp_path / f"crash-{index}"
        child = process(dsn, mode, "synthetic", str(marker))

        try:
            wait_file(marker, child)
        finally:
            child.kill()
            child.communicate(timeout=5)

        with psycopg.connect(dsn) as connection:
            connection.execute("UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second'")

    queue.recover_expired("synthetic")
    view = controls.inspect(actor, context, f"coordinator:{context.run_id}:0")
    assert view.reason == FailureReason.CRASH_LIMIT
    assert view.crashes == state.attempt_limit
    assert view.attempts == state.attempt_limit
    assert "retry" not in view.allowed_actions
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'") == [(0,)]
    assert queue.claim("synthetic", handlers) is None
    assert controls.apply(actor, context, view.job_id, command(view)).decision == "ineligible"
    assert controls.inspect(actor, context, view.job_id).attempts == state.attempt_limit
    other = RunContext("other-tenant", "user", "other-chat", "other-run")
    queue.store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )
    admit(queue, other, state)
    next_lease = claim(queue, handlers)
    assert next_lease.context == other
    Worker(queue, handlers, queue_name="synthetic").execute(next_lease)
    assert queue.store.load_run(other).run.revision == 1


def test_unavailable_handlers_and_stopped_lookup_hold_no_capacity(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    expire(dsn, lease)
    missing = Handlers({})
    assert queue.claim("synthetic", missing) is None
    controls = RecoveryControls(queue, missing, authorize=lambda a, c, action: True)
    view = controls.inspect(actor, context, lease.job_id)
    assert view.reason == FailureReason.HANDLER_UNAVAILABLE
    assert view.allowed_actions == ()
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'") == [(0,)]
    assert (
        controls.apply(actor, context, lease.job_id, command(view)).decision
        == "handler_unavailable"
    )
    second = claim(queue, handlers)
    queue.prepare(second)
    queue.reconcile(second)

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE jobs SET due_at=clock_timestamp()")

    assert queue.claim("synthetic", handlers) is None
    view = RecoveryControls(queue, handlers, authorize=lambda a, c, action: True).inspect(
        actor, context, lease.job_id
    )
    assert view.reason == FailureReason.LOOKUP_UNAVAILABLE
    assert view.status == "stopped"
    assert view.unknown_effect
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'") == [(0,)]


def test_orderly_stop_racing_claim_refunds_without_crash_or_failure(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    stop = Event()

    class StopAfterClaim(PostgresQueue):
        def claim(self, queue: str, handlers: Handlers, *, seconds: float = 30) -> Lease | None:
            lease = super().claim(queue, handlers, seconds=seconds)
            stop.set()
            return lease

    draining = StopAfterClaim(queue.store)

    for _ in range(5):
        stop.clear()
        Worker(draining, handlers, queue_name="synthetic").run(stop)

    view = controls.inspect(actor, context, f"coordinator:{context.run_id}:0")
    assert (view.attempts, view.crashes, view.run_attempts) == (0, 0, 0)
    assert view.reason == FailureReason.NONE
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'") == [(0,)]
    lease = claim(queue, handlers)
    queue.prepare(lease)

    with pytest.raises(StateConflictError, match="prepared"):
        queue.release(lease)


def test_release_lost_ack_is_refunded_once(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    lost = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    lost.release(lease)
    queue.release(lease)
    next_lease = claim(queue, handlers)
    queue.release(lease)
    assert next_lease.attempt == 1
    assert rows(dsn, "SELECT worker_attempts FROM runs") == [(1,)]


@pytest.mark.parametrize("unknown", [False, True])
def test_revoked_approval_is_checked_again_after_accepted_control(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    dsn: str,
    unknown: bool,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)

    if unknown:
        queue.prepare(lease)

    queue.pause(lease, seconds=60, handler=handlers.resolve(state))
    allowed = True
    calls: list[str] = []

    def execute(c: RunContext, s: Recovery):
        calls.append(s.operation_id)
        return complete(c, s)

    current = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: True, execute, idempotent=True, approval_valid=lambda c, s: allowed
            )
        }
    )
    controls = RecoveryControls(queue, current, authorize=lambda a, c, action: True)
    request = command(controls.inspect(actor, context, lease.job_id))
    assert controls.apply(actor, context, lease.job_id, request).decision == "accepted"
    allowed = False
    Worker(queue, current, queue_name="synthetic").execute(claim(queue, current))
    view = controls.inspect(actor, context, lease.job_id)
    assert view.reason == FailureReason.APPROVAL_DENIED
    assert view.unknown_effect == unknown
    assert calls == []
    assert view.allowed_actions == ()
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'") == [(0,)]


def test_completed_effect_and_cancelled_job_cannot_be_reopened(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    Worker(queue, handlers, queue_name="synthetic").execute(lease)
    view = controls.inspect(actor, context, lease.job_id)
    assert view.allowed_actions == ()
    assert not view.unknown_effect
    assert controls.apply(actor, context, lease.job_id, command(view)).decision == "ineligible"
    assert rows(dsn, "SELECT count(*) FROM transitions") == [(1,)]
    next_lease = claim(queue, handlers)
    queue.prepare(next_lease)
    queue.cancel(context)
    view = controls.inspect(actor, context, next_lease.job_id)
    assert view.unknown_effect
    assert view.status == "cancelled_unknown"
    assert (
        controls.apply(actor, context, next_lease.job_id, command(view, "cancelled")).decision
        == "ineligible"
    )
    assert queue.claim("synthetic", handlers) is None


def test_run_budget_cannot_be_reset_to_resume_unknown_work(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    handlers: Handlers,
    dsn: str,
) -> None:
    state = state.model_copy(update={"run_budget": 1})
    admit(queue, context, state)
    lease = claim(queue, handlers)
    queue.prepare(lease)
    expire(dsn, lease)
    assert queue.claim("synthetic", Handlers({})) is None
    handlers = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: True, complete, idempotent=True, reconcile=complete
            )
        }
    )
    controls = RecoveryControls(queue, handlers, authorize=lambda a, c, action: True)
    view = controls.inspect(actor, context, lease.job_id)
    assert view.reason == FailureReason.RUN_BUDGET_EXHAUSTED
    assert view.unknown_effect

    for action in ("retry", "reconcile"):
        assert (
            controls.apply(actor, context, lease.job_id, command(view, action, action)).decision
            == "ineligible"
        )

    assert rows(dsn, "SELECT worker_attempts,worker_budget FROM runs") == [(1, 1)]
    assert queue.claim("synthetic", handlers) is None


def test_control_stales_if_policy_lookup_races_cancellation(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    def access(c: RunContext, s: Recovery) -> bool:
        queue.cancel(c)
        return True

    raced = RecoveryControls(
        queue,
        Handlers({("synthetic", 1): Handler(access, complete)}),
        authorize=lambda a, c, action: True,
    )
    assert raced.apply(actor, context, job_id, request).decision == "stale"


def test_old_timeline_control_is_rejected_by_fresh_adapter(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id='restored-timeline'")

    fresh = RecoveryControls(
        PostgresQueue(PostgresStateStore(dsn)), handlers, authorize=lambda a, c, action: True
    )
    assert fresh.apply(actor, context, job_id, request).decision == "stale"


def test_identical_control_race_returns_one_audited_decision(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    with ThreadPoolExecutor(2) as executor:
        futures = [
            executor.submit(controls.apply, actor, context, job_id, request) for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert results[0] == results[1]
    assert results[0].decision == "accepted"
    assert rows(dsn, "SELECT count(*) FROM worker_controls") == [(1,)]


def test_current_execution_access_denies_recovery(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))
    revoked = RecoveryControls(
        queue,
        Handlers({("synthetic", 1): Handler(lambda c, s: False, complete)}),
        authorize=lambda a, c, action: True,
    )
    assert revoked.apply(actor, context, job_id, request).decision == "access_denied"
    assert revoked.inspect(actor, context, job_id).allowed_actions == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": True},
        {"expected_revision": -1},
        {"action": "reset_budget"},
        {"arguments": {"secret": "PRIVATE"}},
        {"request_id": ""},
    ],
)
def test_control_boundary_rejects_invalid_commands(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ControlRequest.model_validate(
            dict(request_id="request", action="retry", expected_revision=0, recovery_id="timeline")
            | changes
        )


def test_lost_claim_ack_after_sweep_resolves_same_claim(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    failed = False

    def lose_claim_ack(phase: str) -> None:
        nonlocal failed

        if phase == "after_commit" and not failed:
            leased = rows(dsn, "SELECT count(*) FROM jobs WHERE status='leased'")[0][0]

            if leased:
                failed = True
                raise psycopg.OperationalError("synthetic lost claim acknowledgement")

    lost = PostgresQueue(PostgresStateStore(dsn, fault=lose_claim_ack))
    lease = claim(lost, handlers)
    assert failed
    assert lease.attempt == 1
    assert rows(dsn, "SELECT attempt,ownership_token,crash_count FROM jobs") == [(1, 1, 0)]


def test_lost_sweep_ack_does_not_count_crash_twice(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    handlers: Handlers,
    dsn: str,
) -> None:
    admit(queue, context, state)
    lease = claim(queue, handlers)
    expire(dsn, lease)
    lost = PostgresQueue(PostgresStateStore(dsn, fault=Fault(lost_ack=True)))
    lost.recover_expired("synthetic")
    assert rows(dsn, "SELECT attempt,crash_count,status FROM jobs") == [(1, 1, "ready")]


def test_control_database_retry_does_not_repeat_policy_io(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    dsn: str,
) -> None:
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))
    calls: list[str] = []
    failed = False

    def access(c: RunContext, s: Recovery) -> bool:
        calls.append(s.operation_id)
        return True

    def fail(phase: str) -> None:
        nonlocal failed

        if phase == "after_control_write" and not failed:
            failed = True
            raise psycopg.errors.SerializationFailure("synthetic transaction retry")

    handlers = Handlers({("synthetic", 1): Handler(access, complete, idempotent=True)})
    retrying = RecoveryControls(
        PostgresQueue(PostgresStateStore(dsn, fault=fail)),
        handlers,
        authorize=lambda a, c, action: True,
    )
    assert retrying.apply(actor, context, job_id, request).decision == "accepted"
    assert failed
    assert calls == [state.operation_id]
    assert rows(dsn, "SELECT count(*) FROM worker_controls") == [(1,)]


def test_policy_callbacks_cannot_change_the_saved_intent(
    queue: PostgresQueue,
    context: RunContext,
    state: Recovery,
    actor: RecoveryActor,
    controls: RecoveryControls,
    handlers: Handlers,
    dsn: str,
) -> None:
    state = state.model_copy(update={"arguments": {"nested": {"amount": 2}}})
    admit(queue, context, state)
    job_id = f"coordinator:{context.run_id}:0"
    request = command(controls.inspect(actor, context, job_id))

    def mutating_policy(c: RunContext, s: Recovery) -> bool:
        s.arguments["nested"]["amount"] = 3
        return True

    mutated = Handlers({("synthetic", 1): Handler(mutating_policy, complete, idempotent=True)})
    recovery = RecoveryControls(queue, mutated, authorize=lambda a, c, action: True)
    assert recovery.apply(actor, context, job_id, request).decision == "access_denied"
    Worker(queue, mutated, queue_name="synthetic").execute(claim(queue, handlers))
    assert rows(dsn, "SELECT recovery->'arguments' FROM jobs") == [({"nested": {"amount": 2}},)]
    assert rows(dsn, "SELECT failure_reason FROM jobs") == [("access_denied",)]


def test_upgrade_preserves_legacy_unknown_retry_without_authorising_replay(
    dsn: str,
    context: RunContext,
    state: Recovery,
) -> None:
    from uuid import uuid4

    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from agent_harness.storage.codec import fingerprint
    from agent_runtime.storage import postgres

    schema = "legacy_workers_" + uuid4().hex
    legacy = make_conninfo(dsn, options=f"-c search_path={schema}")

    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        with psycopg.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE schema_versions (version text PRIMARY KEY, fingerprint text NOT NULL)"
            )

            for path in sorted(Path(postgres.__file__).with_name("migrations").glob("*.sql")):
                if path.name.startswith("010"):
                    break

                source = path.read_text()
                connection.execute(source.encode())
                connection.execute(
                    "INSERT INTO schema_versions VALUES (%s,%s)", (path.name, fingerprint(source))
                )

            connection.execute("INSERT INTO runtime_generation VALUES (true,'legacy-timeline')")

        old_store = PostgresStateStore(legacy)
        old_store.create_conversation(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )
        old_queue = PostgresQueue(old_store)
        old_queue.configure("synthetic", concurrency=2, tenant_concurrency=1)
        unsafe = state.model_copy(update={"idempotent": False})

        # Seed the pre-010 shape through its existing admission/attachment
        # boundaries. Current queued admission also requires migration 017's
        # release gate, which intentionally does not exist in this old schema.
        old_store.admit(context, request_id=context.run_id, items=INPUT)
        old_queue.attach(context, unsafe)

        with psycopg.connect(legacy) as connection:
            connection.execute(
                "UPDATE jobs SET status='retry',attempt=1, "
                "recovery=jsonb_set(recovery,'{phase}','\"retry\"')"
            )
            connection.execute("UPDATE runs SET worker_attempts=1")

        postgres.migrate(legacy)
        upgraded = PostgresQueue(PostgresStateStore(legacy))
        handlers = Handlers({("synthetic", 1): Handler(lambda c, s: True, complete)})
        assert upgraded.claim("synthetic", handlers) is None
        assert rows(legacy, "SELECT status,effect_pending,attempt FROM jobs") == [
            ("reconcile", True, 1)
        ]
        assert rows(legacy, "SELECT count(*) FROM admissions") == [(1,)]
        assert rows(legacy, "SELECT count(*) FROM transitions") == [(0,)]
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
