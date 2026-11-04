"""Primary admission bounds and fresh-process profile drain/rollback evidence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import httpx2
import pytest

from agent_harness.runs.state import RunStatus
from agent_harness.storage.interface import StateConflictError
from agent_platform.api.queued import create_async_app
from agent_runtime.hosting.access import Principal
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.releases import AdmissionOverloaded, AdmissionPaused, ReleaseControls
from agent_runtime.workers.scaling import pressure
from agent_runtime.workers.state import Handler, Handlers

from .async_process import composition
from .test_postgres import Fault
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import admit as submit
from .test_workers import process
from .test_workers import queue as queue
from .test_workers import state as state
from .worker_process import complete


def owner(context):
    return Principal(context.tenant_id, context.user_id)


def admit(queue, context, state):
    submit(queue, context, state.model_copy(update={"operation_id": context.run_id}))


def controls(store, context):
    return ReleaseControls(store, authorize=lambda actor: actor == owner(context))


def new_chat(store, context, number):
    created = replace(context, conversation_id=f"chat-{number}", run_id=f"run-{number}")
    store.create_conversation(
        tenant_id=created.tenant_id,
        user_id=created.user_id,
        conversation_id=created.conversation_id,
    )
    return created


def test_closed_admission_preserves_replay_and_audits_idempotent_operator_control(
    store, context, queue, state, dsn
):
    admit(queue, context, state)
    operator = owner(context)
    service = controls(store, context)

    with pytest.raises(PermissionError):
        service.change(
            replace(operator, user_id="outsider"),
            request_id="close",
            open=False,
            expected_revision=0,
        )

    # An unknown commit acknowledgement resolves the exact audited decision.
    faulty = controls(PostgresStateStore(dsn, fault=Fault(lost_ack=True)), context)
    assert faulty.change(operator, request_id="close", open=False, expected_revision=0) == 1
    assert service.change(operator, request_id="close", open=False, expected_revision=0) == 1
    admit(queue, context, state)

    with pytest.raises(AdmissionPaused):
        admit(queue, new_chat(store, context, 1), state)

    with pytest.raises(StateConflictError):
        service.change(operator, request_id="close", open=True, expected_revision=0)

    with pytest.raises(StateConflictError):
        service.change(operator, request_id="stale", open=True, expected_revision=0)

    assert service.change(operator, request_id="open", open=True, expected_revision=1) == 2

    with store._connect() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM runtime_release_changes").fetchone()["n"]
            == 2
        )
        assert connection.execute("SELECT count(*) AS n FROM admissions").fetchone()["n"] == 1


def test_concurrent_replicas_cannot_overbook_last_tenant_reservation(store, context, queue, state):
    chats = [new_chat(store, context, index) for index in range(2)]

    with store._connect() as connection:
        connection.execute(
            "UPDATE runtime_admission SET bounded=true,tenant_limit=1,conversation_limit=1"
        )

    barrier = Barrier(2)

    def submit(chat):
        barrier.wait()

        try:
            admit(queue, chat, state)
            return True
        except AdmissionOverloaded:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, chats))

    assert sorted(results) == [False, True]
    accepted = chats[results.index(True)]
    admit(queue, accepted, state)
    queue.cancel(accepted)
    admit(queue, chats[results.index(False)], state)

    with store._connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM admissions").fetchone()["n"] == 2


def test_profile_routing_bypasses_incompatible_backlog_before_candidate_limit(
    store, context, queue, state
):
    old = state.model_copy(update={"arguments": {"profile": "old", "finish": True}})
    new = state.model_copy(update={"arguments": {"profile": "new", "finish": True}})

    for index in range(65):
        admit(queue, new_chat(store, context, index), old)

    admit(queue, context, new)
    handlers = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: True, complete, idempotent=True, required_arguments={"profile": "new"}
            )
        }
    )
    assert pressure(queue, "synthetic", handlers).eligible == 1
    lease = queue.claim("synthetic", handlers)
    assert lease is not None and lease.context == context

    with store._connect() as connection:
        charged = connection.execute("SELECT sum(worker_attempts) AS n FROM runs").fetchone()

    assert charged["n"] == 1


def test_compatible_processes_and_incompatible_drain_then_rollback(
    store, context, queue, state, dsn
):
    service = controls(store, context)
    old = state.model_copy(update={"arguments": {"profile": "old"}})

    # Two compatible independent consumers finish separate chats with unchanged
    # global/tenant limits. Closing admission does not prevent accepted work draining.
    chats = [context, new_chat(store, context, 1)]

    for chat in chats:
        admit(queue, chat, old)

    service.change(owner(context), request_id="drain", open=False, expected_revision=0)

    for _ in range(2):
        children = [process(dsn, "profile", "synthetic", "old") for _ in range(2)]

        for child in children:
            output, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
            assert output.strip() == "settled"

    assert service.inspect(owner(context))["unfinished"] == 0
    assert all(store.load_run(chat).run.status == RunStatus.COMPLETED for chat in chats)
    service.change(owner(context), request_id="new", open=True, expected_revision=1)
    pending = new_chat(store, context, 2)
    new = state.model_copy(update={"arguments": {"profile": "new"}})
    admit(queue, pending, new)
    service.change(owner(context), request_id="rollback-drain", open=False, expected_revision=2)

    # The previous profile cannot consume new pending work during rollback. Keep
    # its compatible consumer alive until it drains, then restart the old profile.
    old_handlers = Handlers(
        {
            ("synthetic", 1): Handler(
                lambda c, s: True, complete, idempotent=True, required_arguments={"profile": "old"}
            )
        }
    )
    assert queue.claim("synthetic", old_handlers) is None

    for _ in range(2):
        child = process(dsn, "profile", "synthetic", "new")
        _, error = child.communicate(timeout=10)
        assert child.returncode == 0, error

    assert service.inspect(owner(context))["unfinished"] == 0
    service.change(owner(context), request_id="rollback", open=True, expected_revision=3)


def test_explicit_http_cancel_checks_owner_and_preserves_stable_result(store, context, sessions):
    runtime, _ = composition(store)

    async def authenticate(request):
        return replace(context, user_id=request.headers.get("x-test-user", context.user_id))

    app = create_async_app(
        runtime, sessions, authenticate=authenticate, prepare_input=lambda c, t: t
    )

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://local"
        ) as client:
            response = await client.post("/runs", json={"request_id": "r", "text": "synthetic"})
            assert response.status_code == 202
            denied = await client.post("/runs/cancel", headers={"x-test-user": "other"})
            assert denied.status_code == 404
            assert store.load_run(context).run.status == RunStatus.QUEUED
            first = await client.post("/runs/cancel")
            second = await client.post("/runs/cancel")
            assert first.status_code == second.status_code == 200
            assert (
                first.json() == second.json() == {"run_id": context.run_id, "status": "cancelled"}
            )

    asyncio.run(run())


@pytest.mark.parametrize("pending", ["model", "compaction", "tool"])
def test_rollback_drains_real_pinned_work_in_fresh_processes(store, context, dsn, pending):
    import os
    import subprocess
    import sys

    from agent_harness.memory.semantic import MemoryBlocked
    from agent_harness.tools.results import VisibleResult

    from .semantic_process import composition as semantic_composition
    from .test_async_groups import fanout, step
    from .test_semantic_memory import assemble, prepare, seed
    from .test_workers import expire, rows

    def worker(mode, queue_name, version):
        result = subprocess.run(
            [sys.executable, "-m", "tests.integration.release_process", mode, queue_name, version],
            env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    original_operation = None

    if pending == "tool":
        runtime, _ = fanout(store, context)
    elif pending == "model":
        runtime, handlers, _, _ = semantic_composition(store)
        asyncio.run(runtime.admit(context, request_id="pending-model", text="Reply briefly."))
        step(runtime, handlers, "model_prepare")
    else:
        seed(store, context, 6)
        parts = prepare(store, context)
        runtime, _, memory, _, lease, _, _ = parts

        class InterruptedSummary:
            async def complete(self, request):
                raise RuntimeError("synthetic interrupted summary transport")

        # Persist a real summary intent and its pinned manifest, then interrupt
        # before publication. Recovery must retain that intent and charged attempt;
        # rolling back the worker cannot reset its budget or substitute a profile.
        memory.summarizer = InterruptedSummary()

        with pytest.raises(MemoryBlocked, match="provider_failed"):
            assemble(parts)

        original_operation = rows(
            dsn, "SELECT operation_id,prompt_manifest,attempts FROM semantic_operations"
        )[0]
        assert original_operation[2] == 1
        expire(dsn, lease)
        runtime.queue.recover_expired("model_infer")

    service = controls(store, context)
    service.change(owner(context), request_id="rollback-close", open=False, expected_revision=0)
    before = rows(dsn, "SELECT sum(worker_attempts) FROM runs")[0][0]
    mode = "tool" if pending == "tool" else "memory"
    queue_name = "tool" if pending == "tool" else "model_infer"
    incompatible = "2" if pending == "tool" else "3001"
    compatible = "1" if pending == "tool" else "3000"

    # A new interpreter with incompatible support must leave accepted work and
    # attempt counts intact. Restore the compatible worker while admission stays
    # closed. The original model, tool and summary operations drain through it.
    assert worker(mode, queue_name, incompatible) == "no_compatible_work"
    assert rows(dsn, "SELECT sum(worker_attempts) FROM runs")[0][0] == before
    assert service.inspect(owner(context))["unfinished"] == 1

    for _ in range(3 if pending == "tool" else 1):
        assert worker(mode, queue_name, compatible) == "settled"

    if pending == "tool":
        assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]
        assert worker("tool", "model_prepare", "1") == "settled"
        assert worker("tool", "model_infer", "1") == "settled"
        checkpoint = store.load_run(context)
        assert (
            len([item for item in checkpoint.conversation.items if isinstance(item, VisibleResult)])
            == 3
        )
    elif original_operation is not None:
        recovered = rows(
            dsn,
            "SELECT operation_id,prompt_manifest,attempts FROM semantic_operations "
            "WHERE result_version IS NOT NULL ORDER BY operation_id",
        )
        assert any(row[:2] == original_operation[:2] and row[2] == 2 for row in recovered)

        with pytest.raises(StateConflictError):
            runtime.queue.prepare(lease)

    assert store.load_run(context).run.status == RunStatus.COMPLETED
    assert service.inspect(owner(context))["unfinished"] == 0
    assert (
        service.change(owner(context), request_id="rollback-open", open=True, expected_revision=1)
        == 2
    )
