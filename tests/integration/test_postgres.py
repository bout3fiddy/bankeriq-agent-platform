"""Synthetic persistence tests against an explicitly configured local PostgreSQL primary."""

import os
import subprocess
import sys
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.model import ModelReply
from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import Checkpoint, StateConflictError, StateNotFoundError
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    WaitReason,
)
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate

from .harness import CONTEXT, PRIVATE_BODY, RuntimeHarness, event_for, inspect_fields

INPUT = (Message(MessageRole.USER, "synthetic input"),)
TABLES = (
    "conversations",
    "runs",
    "admissions",
    "conversation_events",
    "jobs",
    "results",
    "approvals",
    "transitions",
    "projection_work",
)


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("BANKERIQ_TEST_POSTGRES_DSN")

    if not value:
        pytest.skip("set BANKERIQ_TEST_POSTGRES_DSN to a disposable local PostgreSQL database")

    # An explicit DSN opts into a disposable local primary. Apply migrations on
    # each fixture entry to exercise their repeatability; unique tenant IDs below
    # keep each scenario's records separate without deleting another test's data.
    migrate(value)
    return value


@pytest.fixture
def context() -> RunContext:
    return RunContext(str(uuid4()), "user", "chat", "run")


@pytest.fixture
def store(dsn: str, context: RunContext) -> PostgresStateStore:
    result = PostgresStateStore(dsn)
    result.create_conversation(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
    )
    return result


def save(
    store: PostgresStateStore, checkpoint: Checkpoint, status: RunStatus, **kwargs: Any
) -> Checkpoint:
    return store.save_checkpoint(
        checkpoint.run.context,
        expected_conversation_revision=checkpoint.conversation.revision,
        expected_run_revision=checkpoint.run.revision,
        status=status,
        expected_recovery_id=checkpoint.conversation.recovery_id,
        expected_deletion_generation=checkpoint.conversation.deletion_generation,
        **kwargs,
    )


def snapshot(dsn: str, tenant: str) -> list[list[tuple[Any, ...]]]:

    # Several jobs share tenant/conversation/run, so the first three columns do
    # not order them uniquely. Include the fourth identity/sequence column to
    # compare logical rows across rollback and PostgreSQL heap/plan changes.
    with psycopg.connect(dsn) as connection:
        return [
            connection.execute(
                sql.SQL("SELECT * FROM {} WHERE tenant_id = %s ORDER BY 1,2,3,4").format(
                    sql.Identifier(table)
                ),
                (tenant,),
            ).fetchall()
            for table in TABLES
        ]


# Pass Fault(write=3) as the adapter's fault callback to raise after its third
# write, while the transaction can still roll back all related rows.
# Fault(lost_ack=True) raises once after commit: data already exists, so recovery
# must find the same stable operation instead of creating a second result/job.
class Fault:
    def __init__(self, *, write: int | None = None, lost_ack: bool = False) -> None:
        self.write = write
        self.lost_ack = lost_ack
        self.count = 0

    def __call__(self, phase: str) -> None:
        if phase == "after_write":
            self.count += 1

            if self.count == self.write:
                raise RuntimeError("synthetic rollback")

        if phase == "after_commit" and self.lost_ack:
            self.lost_ack = False
            raise psycopg.OperationalError("synthetic lost commit acknowledgement")


def test_admission_retry_queue_context_and_stale_revision(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    first = store.admit(context, request_id="request", items=INPUT)
    running = save(store, first, RunStatus.RUNNING, model_steps=1)
    second_context = replace(context, run_id="second")
    queued = store.admit(
        second_context, request_id="request-2", items=(Message(MessageRole.USER, "later"),)
    )
    assert queued.conversation.active_run_id == context.run_id
    assert store.load_run(context) == running
    assert store.admit(context, request_id="request", items=INPUT) == running

    with pytest.raises(StateConflictError):
        store.admit(context, request_id="request", items=(Message(MessageRole.USER, "changed"),))

    with pytest.raises(StateConflictError):
        save(store, queued, RunStatus.RUNNING)

    with pytest.raises(StateConflictError):
        save(store, first, RunStatus.RUNNING, model_steps=2)

    finished = save(
        store, running, RunStatus.COMPLETED, items=(Message(MessageRole.ASSISTANT, "first answer"),)
    )
    assert finished.conversation.active_run_id == "second"
    restored = PostgresStateStore(dsn).load_run(second_context)
    assert [item.text for item in restored.conversation.items if isinstance(item, Message)] == [
        "synthetic input",
        "first answer",
        "later",
    ]
    assert (
        save(
            store,
            running,
            RunStatus.COMPLETED,
            items=(Message(MessageRole.ASSISTANT, "first answer"),),
        )
        == finished
    )


def test_rollback_at_every_admission_write(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    before = snapshot(dsn, context.tenant_id)

    for boundary in range(1, 8):
        failing = PostgresStateStore(dsn, fault=Fault(write=boundary))

        with pytest.raises(RuntimeError, match="synthetic rollback"):
            failing.admit(context, request_id="request", items=INPUT)

        assert snapshot(dsn, context.tenant_id) == before

    recovered = PostgresStateStore(dsn, fault=Fault(lost_ack=True))
    assert (
        recovered.admit(context, request_id="request", items=INPUT).run.status == RunStatus.QUEUED
    )
    assert len(store.history_page(context).items) == 1


def test_checkpoint_every_write_rollback_and_lost_ack(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    checkpoint = store.admit(context, request_id="request", items=INPUT)
    checkpoint = save(
        store,
        checkpoint,
        RunStatus.RUNNING,
        items=(ToolCall("call", "report", {"public": True}),),
        model_steps=1,
        tool_calls=1,
    )
    pending = PendingToolCall("call", WaitReason.JOB, "job")
    checkpoint = save(
        store, checkpoint, RunStatus.WAITING, items=(pending,), pending_calls=(pending,)
    )
    checkpoint = save(store, checkpoint, RunStatus.RUNNING, pending_calls=(pending,))
    args = {
        "items": (RestrictedReceipt("call"),),
        "artifacts": (ArtifactReference("call", "opaque-report"),),
    }
    before = snapshot(dsn, context.tenant_id)

    # Count the writes in a successful settlement to cover every boundary below.
    counter = Fault()
    observed = PostgresStateStore(dsn, fault=counter)
    completed = save(observed, checkpoint, RunStatus.RUNNING, **args)
    total = counter.count

    # Independent conversations keep committed evidence intact while each boundary fails.
    for boundary in range(1, total + 1):
        other = replace(
            context, conversation_id=f"rollback-{boundary}", run_id=f"rollback-{boundary}"
        )
        store.create_conversation(
            tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
        )
        fresh = store.admit(other, request_id=other.run_id, items=INPUT)
        fresh = save(
            store, fresh, RunStatus.RUNNING, items=(ToolCall("call", "report", {"public": True}),)
        )
        fresh = save(store, fresh, RunStatus.WAITING, items=(pending,), pending_calls=(pending,))
        fresh = save(store, fresh, RunStatus.RUNNING, pending_calls=(pending,))
        original = snapshot(dsn, context.tenant_id)

        with pytest.raises(RuntimeError, match="synthetic rollback"):
            save(
                PostgresStateStore(dsn, fault=Fault(write=boundary)),
                fresh,
                RunStatus.RUNNING,
                **args,
            )

        assert snapshot(dsn, context.tenant_id) == original

    assert snapshot(dsn, context.tenant_id) != before
    assert (
        save(
            PostgresStateStore(dsn, fault=Fault(lost_ack=True)),
            checkpoint,
            RunStatus.RUNNING,
            **args,
        )
        == completed
    )
    assert store.deliveries(context) == (ArtifactReference("call", "opaque-report"),)
    assert "opaque-report" not in inspect_fields(store.load_run(context).conversation)


def test_terminal_activation_rolls_back_together(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    first = save(store, store.admit(context, request_id="first", items=INPUT), RunStatus.RUNNING)
    store.admit(replace(context, run_id="second"), request_id="second", items=INPUT)
    before = snapshot(dsn, context.tenant_id)

    for boundary in range(1, 10):
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            save(
                PostgresStateStore(dsn, fault=Fault(write=boundary)),
                first,
                RunStatus.COMPLETED,
                items=(Message(MessageRole.ASSISTANT, "done"),),
            )

        assert snapshot(dsn, context.tenant_id) == before


PROCESS = """
import os, sys
from agent_runtime.runs.context import RunContext
from agent_runtime.conversations.messages import Message, MessageRole
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
s = PostgresStateStore(os.environ['BANKERIQ_TEST_POSTGRES_DSN'])
c = RunContext(sys.argv[1], 'user', 'chat', sys.argv[2])
s.admit(c, request_id=sys.argv[2], items=(Message(MessageRole.USER, sys.argv[2]),))
"""


def test_independent_process_admission_and_restart(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    environment = {**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", PROCESS, context.tenant_id, run],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for run in ("first", "first", "second", "third")
    ]

    for process in processes:
        _, error = process.communicate(timeout=20)
        assert process.returncode == 0, error

    # A new interpreter sees the admitted input and its work together after all
    # admitting processes exit. No inherited Python state supplies either record.
    check = """
import os, sys
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from agent_runtime.runs.context import RunContext
import psycopg
s = PostgresStateStore(os.environ['BANKERIQ_TEST_POSTGRES_DSN'])
with psycopg.connect(os.environ['BANKERIQ_TEST_POSTGRES_DSN']) as db:
    rows = db.execute("SELECT run_id, admission_sequence, active FROM runs "
                      "WHERE tenant_id = %s ORDER BY admission_sequence",
                      (sys.argv[1],)).fetchall()
    assert len(rows) == 3 and sum(row[2] for row in rows) == 1
    for run, sequence, active in rows:
        c = RunContext(sys.argv[1], 'user', 'chat', run)
        assert s.load_run(c).conversation.items[-1].text == run
        job = db.execute("SELECT status FROM jobs WHERE tenant_id = %s AND run_id = %s",
                         (sys.argv[1], run)).fetchone()
        assert job[0] == ('ready' if active else 'blocked')
print('input and work survived restart')
"""
    result = subprocess.run(
        [sys.executable, "-c", check, context.tenant_id],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "input and work survived restart" in result.stdout


def test_tenant_owner_and_bounds(store: PostgresStateStore, context: RunContext) -> None:
    store.admit(context, request_id="request", items=INPUT)

    for intruder in (replace(context, tenant_id="other"), replace(context, user_id="other")):
        with pytest.raises(StateNotFoundError):
            store.load_run(intruder)

        with pytest.raises(StateNotFoundError):
            store.deliveries(intruder)

        with pytest.raises(StateNotFoundError):
            store.admit(intruder, request_id="request", items=INPUT)

    with pytest.raises(ValueError):
        store.admit(
            replace(context, run_id="large"),
            request_id="large",
            items=(Message(MessageRole.USER, "x" * 70000),),
        )

    assert len(store.history_page(context, limit=1).items) == 1


def test_real_coordinator_continuation_output_isolation(dsn: str) -> None:
    runtime = RuntimeHarness()
    tenant = str(uuid4())
    context = replace(CONTEXT, tenant_id=tenant)
    store = PostgresStateStore(dsn)
    store.create_conversation(
        tenant_id=tenant, user_id=context.user_id, conversation_id=context.conversation_id
    )
    runtime.access.permissions[(tenant, context.user_id)] = runtime.access.permissions[
        (CONTEXT.tenant_id, CONTEXT.user_id)
    ]
    runtime.access.datasets[(tenant, context.user_id)] = frozenset({"sample"})
    coordinator = Coordinator(
        store,
        runtime.registry,
        runtime.events,
        runtime.model,
        runtime.access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )
    handler = ContinuationHandler(store, coordinator, runtime.events)
    runtime.model.replies = [
        ModelReply(calls=(ToolCall("report", "build_report", {"dataset": "sample", "total": 42}),)),
        ModelReply(answer="done"),
    ]
    waiting = coordinator.start(context, INPUT[0], expected_conversation_revision=0)
    approval = waiting.run.pending_calls[0]
    decision = runtime.events.decide(context, approval, granted=True)
    waiting = handler.resume(context, decision)
    result = handler.resume(context, event_for(waiting.run.pending_calls[0]))
    assert result.run.status == RunStatus.COMPLETED
    assert PostgresStateStore(dsn).deliveries(context) == result.artifacts
    assert PRIVATE_BODY not in str(snapshot(dsn, tenant))
    assert all(
        PRIVATE_BODY not in payload and result.artifacts[0].artifact_id not in payload
        for payload in runtime.model.request_payloads
    )


def test_deletion_and_recovery_fence(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    checkpoint = store.admit(context, request_id="request", items=INPUT)
    before = snapshot(dsn, context.tenant_id)

    for boundary in range(1, 5):
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            PostgresStateStore(dsn, fault=Fault(write=boundary)).delete_conversation(context)

        assert snapshot(dsn, context.tenant_id) == before

    assert PostgresStateStore(dsn, fault=Fault(lost_ack=True)).delete_conversation(context) == 1
    assert store.delete_conversation(context) == 1

    with pytest.raises(StateNotFoundError):
        save(store, checkpoint, RunStatus.RUNNING)

    # This emulates an operator cutover on the disposable primary after writers
    # are quiesced. A fresh unique ID prevents a restored counter from repeating.
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id = %s", (str(uuid4()),))

    with pytest.raises(StateConflictError, match="recovery generation"):
        store.history_page(context)


def test_approval_decision_and_pending_rollback(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    call = ToolCall("call", "report", {"public": 42})
    checkpoint = save(
        store,
        store.admit(context, request_id="request", items=INPUT),
        RunStatus.RUNNING,
        items=(call,),
    )
    pending = PendingToolCall("call", WaitReason.APPROVAL, "approval")
    before = snapshot(dsn, context.tenant_id)

    for boundary in range(1, 9):
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            save(
                PostgresStateStore(dsn, fault=Fault(write=boundary)),
                checkpoint,
                RunStatus.WAITING,
                items=(pending,),
                pending_calls=(pending,),
            )

        assert snapshot(dsn, context.tenant_id) == before

    waiting = save(
        PostgresStateStore(dsn, fault=Fault(lost_ack=True)),
        checkpoint,
        RunStatus.WAITING,
        items=(pending,),
        pending_calls=(pending,),
    )
    assert waiting.run.pending_calls == (pending,)
    before = snapshot(dsn, context.tenant_id)
    decision = {
        "reference_id": "approval",
        "call": call,
        "operation_version": "v1",
        "granted": True,
        "expires_at": "2099-01-01T00:00:00Z",
    }

    for boundary in range(1, 4):
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            PostgresStateStore(dsn, fault=Fault(write=boundary)).record_approval(
                context, **decision
            )

        assert snapshot(dsn, context.tenant_id) == before

    PostgresStateStore(dsn, fault=Fault(lost_ack=True)).record_approval(context, **decision)
    store.record_approval(context, **decision)

    with pytest.raises(StateConflictError):
        store.record_approval(context, **{**decision, "granted": False})

    with pytest.raises(StateConflictError):
        store.record_approval(
            context, **{**decision, "call": replace(call, arguments={"public": 43})}
        )


def test_lost_ack_on_new_settlement_and_generation_reconnect(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    checkpoint = store.admit(context, request_id="request", items=INPUT)
    running = save(store, checkpoint, RunStatus.RUNNING, items=(ToolCall("call", "report", {}),))
    completed = save(
        PostgresStateStore(dsn, fault=Fault(lost_ack=True)),
        running,
        RunStatus.COMPLETED,
        items=(RestrictedReceipt("call"),),
        artifacts=(ArtifactReference("call", "opaque"),),
    )
    assert completed.run.status == RunStatus.COMPLETED

    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM results WHERE tenant_id = %s", (context.tenant_id,)
        ).fetchone() == (1,)
        connection.execute("UPDATE runtime_generation SET recovery_id = %s", (str(uuid4()),))

    with pytest.raises(StateConflictError, match="recovery generation"):
        save(
            PostgresStateStore(dsn),
            running,
            RunStatus.COMPLETED,
            items=(RestrictedReceipt("call"),),
            artifacts=(ArtifactReference("call", "opaque"),),
        )


def test_process_dies_after_commit_before_acceptance(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    script = PROCESS.replace(
        "s = PostgresStateStore(os.environ['BANKERIQ_TEST_POSTGRES_DSN'])",
        "s = PostgresStateStore(os.environ['BANKERIQ_TEST_POSTGRES_DSN'], "
        "fault=lambda phase: os._exit(17) if phase == 'after_commit' else None)",
    )
    result = subprocess.run(
        [sys.executable, "-c", script, context.tenant_id, context.run_id],
        env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
        timeout=20,
    )
    assert result.returncode == 17
    recovered = store.admit(
        context, request_id=context.run_id, items=(Message(MessageRole.USER, context.run_id),)
    )
    assert recovered.run.status == RunStatus.QUEUED

    with psycopg.connect(dsn) as connection:
        for table in ("admissions", "conversation_events", "runs", "jobs", "projection_work"):
            assert connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(
                    sql.Identifier(table)
                ),
                (context.tenant_id,),
            ).fetchone() == (1,)


def test_history_cursor_tracks_metadata_gaps(
    store: PostgresStateStore, context: RunContext
) -> None:
    checkpoint = store.admit(context, request_id="request", items=INPUT)
    checkpoint = save(store, checkpoint, RunStatus.RUNNING)
    save(store, checkpoint, RunStatus.COMPLETED, items=(Message(MessageRole.ASSISTANT, "done"),))
    first = store.history_page(context, limit=1)
    second = store.history_page(context, after=first.next_cursor, limit=1)
    end = store.history_page(context, after=second.next_cursor)
    assert first.items + second.items == INPUT + (Message(MessageRole.ASSISTANT, "done"),)
    assert second.next_cursor > first.next_cursor + 1
    assert not end.items and end.next_cursor == end.durable_cursor


def test_coordinator_queues_later_input_and_reuses_finished_outcome(
    store: PostgresStateStore, context: RunContext
) -> None:
    runtime = RuntimeHarness()
    coordinator = Coordinator(
        store,
        runtime.registry,
        runtime.events,
        runtime.model,
        runtime.access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )
    first = store.admit(context, request_id=context.run_id, items=INPUT)
    later = replace(context, run_id="later")
    queued = coordinator.start(
        later,
        Message(MessageRole.USER, "later"),
        expected_conversation_revision=first.conversation.revision,
    )
    assert queued.run.status == RunStatus.QUEUED and not runtime.model.requests
    runtime.model.replies = [ModelReply(answer="first answer"), ModelReply(answer="second answer")]
    coordinator.run(context)
    coordinator.run(later)
    repeated = coordinator.start(
        later, Message(MessageRole.USER, "later"), expected_conversation_revision=0
    )
    assert repeated.answer == "second answer"
    assert len(runtime.model.requests) == 2
    assert runtime.model.requests[0].history == INPUT
    assert runtime.model.requests[1].history == INPUT + (
        Message(MessageRole.ASSISTANT, "first answer"),
        Message(MessageRole.USER, "later"),
    )
