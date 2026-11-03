"""Rehearsal cutover must fence old leases and reject unresolved effects."""

from uuid import uuid4

import pytest

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.state import RunStatus
from agent_harness.storage.interface import StateConflictError
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.queue import PostgresQueue
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery
from tests.azure.recovery import prepare_restore

from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url


def test_restore_fences_leases_and_is_repeatable(store, dsn, context):
    queue = PostgresQueue(store)
    queue.configure("restore", concurrency=1, tenant_concurrency=1)
    queue.admit(
        context,
        request_id="input",
        items=(Message(MessageRole.USER, "synthetic input"),),
        state=Recovery(kind="restore", operation_id="prepare", idempotent=True),
    )
    handlers = Handlers(
        {("restore", 1): Handler(lambda c, s: True, lambda c, s: Completion(RunStatus.COMPLETED))}
    )
    lease = queue.claim("restore", handlers)
    assert lease is not None
    before = store.load_run(context).run.status
    generation = str(uuid4())
    source = PrimaryConnections(dsn)
    assert prepare_restore(source, generation)
    assert not prepare_restore(source, generation)

    with pytest.raises(StateConflictError):
        queue.renew(lease)

    fresh = PostgresStateStore(dsn)
    assert fresh.recovery_id == generation
    assert fresh.load_run(context).run.status == before


def test_restore_refuses_unreconciled_effects(store, dsn, context):
    queue = PostgresQueue(store)
    queue.configure("restore", concurrency=1, tenant_concurrency=1)
    queue.admit(
        context,
        request_id="input",
        items=(Message(MessageRole.USER, "synthetic input"),),
        state=Recovery(kind="restore", operation_id="effect"),
    )
    handlers = Handlers(
        {("restore", 1): Handler(lambda c, s: True, lambda c, s: Completion(RunStatus.COMPLETED))}
    )
    lease = queue.claim("restore", handlers)
    assert lease is not None
    queue.prepare(lease)

    with pytest.raises(StateConflictError, match="reconcile unknown effects"):
        prepare_restore(PrimaryConnections(dsn), str(uuid4()))

    assert PostgresStateStore(dsn).recovery_id == store.recovery_id
