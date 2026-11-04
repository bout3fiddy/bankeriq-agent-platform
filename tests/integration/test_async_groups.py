"""Real-service asynchronous fan-out, cancellation, recovery and delivery gates."""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from agent_harness.conversations.messages import Message
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunStatus
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_harness.tools.results import ToolErrorCode, ToolFailure, VisibleResult
from agent_runtime.sessions.stream import ProgressStream
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.controls import ControlRequest, RecoveryActor, RecoveryControls
from agent_runtime.workers.state import Completion, Handlers

from .async_process import composition
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import expire, rows, wait_file


def step(runtime: Any, handlers: Handlers, kind: str):
    lease = runtime.queue.claim(kind, handlers, seconds=10)
    assert lease is not None
    asyncio.run(AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(lease))
    return lease


def fanout(store: PostgresStateStore, context: RunContext, *, fail_fast: bool = False):
    runtime, handlers = composition(store, fail_fast=fail_fast)
    assert (
        asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
        == context.run_id
    )
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    assert store.load_run(context).run.status == RunStatus.WAITING
    assert runtime.queue.claim("model_infer", handlers) is None
    return runtime, handlers


def test_overlap_order_duplicate_and_single_join(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(store, context)
    barrier = Barrier(3)
    leases = [runtime.queue.claim("tool", handlers, seconds=10) for _ in range(3)]
    assert all(leases)

    def finish(index: int):
        lease = leases[index]
        assert lease is not None
        runtime.queue.prepare(lease)
        barrier.wait(timeout=5)
        time.sleep((2 - index) * 0.05)
        completion = handlers.resolve(lease.state).execute(context, lease.state)
        runtime.queue.settle(lease, completion)
        runtime.queue.settle(lease, completion)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(finish, range(3)))

    result = store.load_run(context)
    assert [
        item.call_id for item in result.conversation.items if isinstance(item, VisibleResult)
    ] == ["call-0", "call-1", "call-2"]
    assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]
    assert rows(
        dsn, "SELECT count(*) FROM jobs WHERE operation='model_prepare' AND status='ready'"
    ) == [(1,)]
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    assert store.load_run(context).run.status == RunStatus.COMPLETED
    assert store.load_run(context).run.tool_calls == 3
    assert store.load_run(context).run.model_steps == 2


@pytest.mark.parametrize("phase", ["admitted", "claimed", "dispatched", "settling"])
def test_cancel_fences_all_children(
    store: PostgresStateStore, context: RunContext, dsn: str, phase: str
) -> None:
    runtime, handlers = fanout(store, context)
    lease = None

    if phase != "admitted":
        lease = runtime.queue.claim("tool", handlers)
        assert lease is not None

        if phase in {"dispatched", "settling"}:
            runtime.queue.prepare(lease)

    runtime.queue.cancel(context)
    assert runtime.queue.claim("tool", handlers) is None
    assert runtime.queue.claim("model_prepare", handlers) is None

    if lease:
        with pytest.raises(StateConflictError):
            runtime.queue.settle(
                lease, Completion(RunStatus.RUNNING, (VisibleResult("call-0", 5),))
            )

    assert rows(dsn, "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL AND run_id='run'") == [
        (3,)
    ]
    unknown = rows(dsn, "SELECT count(*) FROM jobs WHERE status='cancelled_unknown'")[0][0]
    assert unknown == (1 if phase in {"dispatched", "settling"} else 0)
    assert not rows(dsn, "SELECT 1 FROM tool_groups WHERE joined")


@pytest.mark.parametrize("fail_fast", [False, True])
def test_sibling_failure_policies(
    store: PostgresStateStore, context: RunContext, dsn: str, fail_fast: bool
) -> None:
    runtime, handlers = fanout(store, context, fail_fast=fail_fast)
    first = runtime.queue.claim("tool", handlers)
    second = runtime.queue.claim("tool", handlers)
    assert first and second
    runtime.queue.prepare(second)
    runtime.queue.settle(
        first,
        Completion(
            RunStatus.RUNNING,
            (ToolFailure(first.state.arguments["call"]["call_id"], ToolErrorCode.DENIED),),
        ),
    )

    if fail_fast:
        assert store.load_run(context).run.status == RunStatus.FAILED
        assert rows(dsn, "SELECT count(*) FROM jobs WHERE status='cancelled_unknown'") == [(1,)]

        with pytest.raises(StateConflictError):
            runtime.queue.prepare(second)
    else:
        assert store.load_run(context).run.status == RunStatus.WAITING
        runtime.queue.settle(second, handlers.resolve(second.state).execute(context, second.state))
        step(runtime, handlers, "tool")
        assert store.load_run(context).run.status == RunStatus.RUNNING


def test_resource_serialization_and_unknown_effect(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = composition(store)
    bind = runtime.bind
    runtime.bind = lambda *args: replace(bind(*args), resource_key="account-17")
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    first = runtime.queue.claim("tool", handlers)
    assert first is not None
    assert runtime.queue.claim("tool", handlers) is None
    runtime.queue.prepare(first)
    runtime.queue.reconcile(first)
    assert runtime.queue.claim("tool", handlers) is None

    # Reconciliation has no lookup registration, so it stays inspectable and
    # holds the resource against later conflicting effects, without a lease.
    controls = RecoveryControls(runtime.queue, handlers, authorize=lambda a, c, op: True)
    view = controls.inspect(RecoveryActor(context.tenant_id, "operator"), context, first.job_id)
    assert view.unknown_effect and view.group_id and view.parent_run_id == context.run_id
    assert view.call_id == first.state.arguments["call"]["call_id"]


def test_child_recovery_controls_and_scoped_inspection(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(store, context)
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    expire(dsn, lease)
    runtime.queue.recover_expired("tool")
    controls = RecoveryControls(runtime.queue, handlers, authorize=lambda a, c, op: True)
    actor = RecoveryActor(context.tenant_id, "operator")
    view = controls.inspect(actor, context, lease.job_id)
    assert view.crashes == 1 and view.reason == "process_lost"
    assert view.unknown_effect and "retry" in view.allowed_actions
    receipt = controls.apply(
        actor,
        context,
        lease.job_id,
        ControlRequest(
            request_id="recover-child",
            action="retry",
            expected_revision=view.revision,
            recovery_id=view.recovery_id,
        ),
    )
    assert receipt.decision == "accepted"
    recovered_leases = [runtime.queue.claim("tool", handlers) for _ in range(3)]
    recovered = next(
        item for item in recovered_leases if item is not None and item.job_id == lease.job_id
    )
    assert recovered.state.operation_id == lease.state.operation_id
    assert recovered.attempt == 2

    for item in recovered_leases:
        assert item is not None
        asyncio.run(AsyncWorker(runtime.queue, handlers, queue_name="tool").execute_async(item))

    assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]

    with pytest.raises(PermissionError):
        controls.inspect(RecoveryActor("other", "operator"), context, lease.job_id)

    with pytest.raises(StateNotFoundError):
        store.load_run(replace(context, user_id="intruder"))


def test_independent_process_overlap_restart_and_durable_reconnect(
    store: PostgresStateStore,
    context: RunContext,
    dsn: str,
    tmp_path: Path,
    sessions: Any,
) -> None:
    runtime, handlers = fanout(store, context)
    children = []

    def launch(index: int):
        marker, release = tmp_path / f"entered-{index}", tmp_path / f"release-{index}"
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
        children.append(child)
        wait_file(marker, child)
        return child, marker, release

    try:
        first, marker_a, _ = launch(0)
        second, marker_b, release_b = launch(1)
        assert marker_a.read_text() != marker_b.read_text()
        assert rows(
            dsn, "SELECT count(*) FROM jobs WHERE operation='tool' AND status='leased'"
        ) == [(2,)]
        first.kill()
        first.communicate(timeout=5)
        release_b.touch()
        output, error = second.communicate(timeout=5)
        assert second.returncode == 0, error
        assert "settled" in output
        time.sleep(0.6)
        third, _, release_c = launch(2)
        release_c.touch()
        _, error = third.communicate(timeout=5)
        assert third.returncode == 0, error
        step(runtime, handlers, "tool")
        assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]
        step(runtime, handlers, "model_prepare")
        step(runtime, handlers, "model_infer")
        sessions.cache.client.flushdb()
        stream = ProgressStream(sessions, context)
        frames = []

        for _ in range(20):
            stream.poll()

            while (frame := stream.buffer.take()) is not None:
                frames.append(frame)

        assert b"Synthetic total saved" in b"".join(frames)
        assert store.load_run(context).run.status == RunStatus.COMPLETED
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_slow_consumer_bounds_and_final_recovery(
    store: PostgresStateStore, context: RunContext, sessions: Any
) -> None:
    runtime, handlers = fanout(store, context)
    stream = ProgressStream(sessions, context, max_events=1, max_bytes=512)
    stream.poll()
    assert stream.buffer.overflow
    assert stream.buffer.size <= 512 and len(stream.buffer.events) == 1

    for _ in range(3):
        step(runtime, handlers, "tool")

    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    sessions.cache.client.flushdb()
    recovered = ProgressStream(sessions, context)
    data = []

    for _ in range(30):
        recovered.poll()

        while (frame := recovered.buffer.take()) is not None:
            data.append(frame)

    assert b"Synthetic total saved" in b"".join(data)
    assert not recovered.buffer.overflow
    assert recovered.cursor is not None
    reconnect = ProgressStream(sessions, context, cursor=recovered.cursor)
    reconnect.poll()
    frame = reconnect.buffer.take()
    assert frame is not None and frame.startswith(b"event: final")
    assert b"Synthetic total saved" in frame


def test_cancel_races_last_settlement(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(store, context)
    step(runtime, handlers, "tool")
    step(runtime, handlers, "tool")
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    barrier = Barrier(2)

    def settle():
        barrier.wait(timeout=5)

        with contextlib.suppress(StateConflictError):
            runtime.queue.settle(lease, handlers.resolve(lease.state).execute(context, lease.state))

    def cancel():
        barrier.wait(timeout=5)
        runtime.queue.cancel(context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [settle, cancel]))

    assert store.load_run(context).run.status == RunStatus.CANCELLED
    assert runtime.queue.claim("model_prepare", handlers) is None
    assert runtime.queue.claim("tool", handlers) is None


def test_child_attempt_budget_and_revoked_access(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = fanout(store, context)
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    expire(dsn, lease)
    runtime.queue.recover_expired("tool")
    entries = dict(handlers.entries)
    entries["tool", 1] = replace(entries["tool", 1], authorize=lambda c, s: False)
    denied = Handlers(entries)

    for _ in range(3):
        step(runtime, denied, "tool")

    assert rows(
        dsn,
        "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL AND failure_reason='access_denied'",
    ) == [(3,)]
    assert store.load_run(context).run.status == RunStatus.RUNNING
    assert rows(dsn, "SELECT worker_attempts FROM runs") == [(6,)]


def test_approval_wait_releases_slot_and_retains_call(
    store: PostgresStateStore, context: RunContext
) -> None:
    runtime, handlers = fanout(store, context)
    entries = dict(handlers.entries)
    entries["tool", 1] = replace(entries["tool", 1], approval_required=lambda c, s: True)
    approval_handlers = Handlers(entries)
    lease = step(runtime, approval_handlers, "tool")
    step(runtime, handlers, "tool")
    step(runtime, handlers, "tool")
    assert runtime.queue.approve(context, lease.job_id, lease.state) is None
    step(runtime, approval_handlers, "tool")
    assert store.load_run(context).run.status == RunStatus.RUNNING


def test_async_provider_batch_and_interrupted_step_reuses_manifest(
    store: PostgresStateStore,
    context: RunContext,
    dsn: str,
) -> None:
    import httpx2
    from openai import AsyncOpenAI

    from agent_runtime.providers.foundry.model import AsyncFoundryModelClient

    from .test_foundry import _response

    requests = []

    async def respond(request: httpx2.Request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["parallel_tool_calls"] is True

        if len(requests) == 1:
            raise httpx2.ReadTimeout("PRIVATE_PROVIDER_MARKER", request=request)

        return _response(
            "tool_calls",
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"provider-{index}",
                        "type": "function",
                        "function": {"name": "calculate", "arguments": '{"values":[2,3]}'},
                    }
                    for index in range(3)
                ],
            },
        )

    async def run():
        runtime, handlers = composition(store)
        await runtime.admit(context, request_id="request", text="calculate")
        lease = runtime.queue.claim("model_prepare", handlers)
        assert lease is not None
        await AsyncWorker(runtime.queue, handlers, queue_name="model_prepare").execute_async(lease)
        pinned = store.load_run(context).run.prompt_manifest

        async with AsyncOpenAI(
            base_url="https://synthetic.openai.azure.com/openai/v1/",
            api_key="synthetic",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            runtime.model = AsyncFoundryModelClient(client, deployment="synthetic")
            inference = runtime.queue.claim("model_infer", handlers)
            assert inference is not None
            worker = AsyncWorker(runtime.queue, handlers, queue_name="model_infer")
            await worker.execute_async(inference)
            assert store.load_run(context).run.prompt_manifest == pinned
            await asyncio.sleep(1.05)
            retry = runtime.queue.claim("model_infer", handlers)
            assert retry is not None and retry.attempt == 2
            await worker.execute_async(retry)
            assert store.load_run(context).run.status == RunStatus.WAITING
            assert store.load_run(context).run.model_steps == 1
            assert requests[0]["messages"] == requests[1]["messages"]
            assert "PRIVATE_PROVIDER_MARKER" not in str(rows(dsn, "SELECT recovery FROM jobs"))

    asyncio.run(run())


def test_idle_wait_and_slow_model_do_not_block_another_conversation(
    store: PostgresStateStore,
    context: RunContext,
) -> None:
    from agent_harness.coordinator.model import ModelReply

    async def run():
        runtime, handlers = composition(store)
        second = replace(context, conversation_id="second", run_id="second")
        store.create_conversation(
            tenant_id=second.tenant_id,
            user_id=second.user_id,
            conversation_id=second.conversation_id,
        )
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowModel:
            async def complete(self, request):
                if request.history[-1].text == "slow":
                    entered.set()
                    await release.wait()

                return ModelReply(answer="saved")

        runtime.model = SlowModel()
        await runtime.admit(context, request_id="slow", text="slow")
        await runtime.admit(second, request_id="fast", text="fast")

        for _ in range(2):
            lease = runtime.queue.claim("model_prepare", handlers)
            assert lease is not None
            await AsyncWorker(runtime.queue, handlers, queue_name="model_prepare").execute_async(
                lease
            )

        slow = runtime.queue.claim("model_infer", handlers)
        assert slow is not None
        worker = AsyncWorker(runtime.queue, handlers, queue_name="model_infer")
        task = asyncio.create_task(worker.execute_async(slow))
        await asyncio.wait_for(entered.wait(), 3)
        fast = runtime.queue.claim("model_infer", handlers)
        assert fast is not None
        await worker.execute_async(fast)
        assert store.load_run(second).run.status == RunStatus.COMPLETED
        release.set()
        await task

    asyncio.run(run())


def test_disconnect_cancels_reader_only(
    store: PostgresStateStore, context: RunContext, sessions: Any
) -> None:
    runtime, handlers = fanout(store, context)

    async def disconnect():
        stream = ProgressStream(sessions, context, poll_seconds=0.01)
        reader = stream.events()
        assert await anext(reader)
        await reader.aclose()

    asyncio.run(disconnect())

    for _ in range(3):
        step(runtime, handlers, "tool")

    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    assert store.load_run(context).run.status == RunStatus.COMPLETED


def test_restricted_payload_is_absent_from_model_journal_and_progress(
    store: PostgresStateStore,
    context: RunContext,
    dsn: str,
    sessions: Any,
) -> None:
    from agent_harness.tools.bindings import ExecutionOutput, ToolBinding
    from agent_harness.tools.definitions import OutputPolicy
    from agent_harness.tools.gateway import ToolGateway
    from agent_harness.tools.registry import ToolRegistry
    from agent_harness.tools.results import RestrictedReceipt

    from .harness import CALCULATE, PERMISSIONS, FakeAccess

    runtime, handlers = composition(store)
    registry = ToolRegistry((replace(CALCULATE, output_policy=OutputPolicy.RESTRICTED),))
    access = FakeAccess()
    access.permissions_for = lambda context: PERMISSIONS
    runtime.gateway = ToolGateway(
        registry,
        {
            ("calculate", "v1"): ToolBinding(
                lambda c, args: ExecutionOutput(
                    "PRIVATE_TOOL_MARKER", artifact_id="https://reports.invalid/synthetic-report"
                ),
                lambda args: None,
            )
        },
        access,
    )
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")

    for _ in range(3):
        step(runtime, handlers, "tool")

    checkpoint = store.load_run(context)
    assert (
        len([item for item in checkpoint.conversation.items if isinstance(item, RestrictedReceipt)])
        == 3
    )
    assert len(store.deliveries(context)) == 3
    step(runtime, handlers, "model_prepare")
    request, _ = runtime.coordinator.prepare_request(store.load_run(context))
    from .harness import inspect_fields

    for table in ("conversation_events", "jobs", "results", "transitions"):
        assert "PRIVATE_TOOL_MARKER" not in str(rows(dsn, f"SELECT * FROM {table}"))

    assert "synthetic-report" not in inspect_fields(request)
    assert "PRIVATE_TOOL_MARKER" not in json.dumps(sessions.read(context))


def test_cancellation_races_atomic_fanout(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = composition(store)
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    completion = asyncio.run(runtime.infer_leased(context, lease.state, lease))
    barrier = Barrier(2)

    def fan_out():
        barrier.wait(timeout=5)

        with contextlib.suppress(StateConflictError):
            runtime.queue.settle(lease, completion)

    def cancel():
        barrier.wait(timeout=5)
        runtime.queue.cancel(context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [fan_out, cancel]))

    assert store.load_run(context).run.status == RunStatus.CANCELLED
    count = rows(dsn, "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL")[0][0]
    assert count in (0, 3)
    assert not rows(dsn, "SELECT 1 FROM jobs WHERE group_id IS NOT NULL AND status='ready'")
    assert runtime.queue.claim("tool", handlers) is None


def test_tool_budget_and_per_child_crash_limit(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = composition(store)
    bind = runtime.bind
    runtime.bind = lambda *args: replace(
        bind(*args), recovery=bind(*args).recovery.model_copy(update={"attempt_limit": 1})
    )
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    expire(dsn, lease)
    runtime.queue.recover_expired("tool")
    step(runtime, handlers, "tool")
    step(runtime, handlers, "tool")
    assert store.load_run(context).run.status == RunStatus.RUNNING
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL AND attempt=1") == [(3,)]


@pytest.mark.parametrize("budget", [None, 8])
def test_admission_transport_and_input_policy(
    store: PostgresStateStore, context: RunContext, sessions: Any, budget: int | None
) -> None:
    import httpx2

    from agent_platform.api.queued import create_async_app

    runtime, _ = composition(store)

    async def authenticate(request):
        if request.headers.get("x-test-user") == "other":
            return replace(context, user_id="other")

        return context

    app = create_async_app(
        runtime,
        sessions,
        authenticate=authenticate,
        prepare_input=lambda c, text: "permitted " + text,
    )

    async def request():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://local"
        ) as client:
            payload: dict[str, Any] = {"request_id": "request", "text": "input"}

            if budget is not None:
                payload["worker_budget"] = budget

            result = await client.post("/runs", json=payload)
            assert result.status_code == 202
            assert result.json() == {"accepted": True, "run_id": context.run_id}
            duplicate = await client.post("/runs", json=payload)
            assert duplicate.json() == result.json()
            changed_budget = await client.post("/runs", json={**payload, "worker_budget": 9})
            assert changed_budget.status_code == 409

            for invalid_budget in (0, 101, True):
                invalid = await client.post(
                    "/runs", json={**payload, "worker_budget": invalid_budget}
                )
                assert invalid.status_code == 400

            bad = await client.post("/runs", json={"request_id": "request", "text": "other"})
            assert bad.status_code == 409
            activity = await client.post("/activity")
            assert activity.status_code == 200
            history = await client.get("/history")
            assert history.status_code == 200 and history.json()["events"]

            # Authentication alone does not grant access to another user's chat.
            # Reject both retention signals and history before touching its cache.
            for method, path in (("POST", "/activity"), ("GET", "/history")):
                denied = await client.request(method, path, headers={"x-test-user": "other"})
                assert denied.status_code == 404
                assert denied.json() == {"error": "session_unavailable"}

    asyncio.run(request())
    assert store.load_run(context).run.status == RunStatus.QUEUED

    with store._connect() as connection:
        saved = connection.execute(
            "SELECT worker_budget FROM runs WHERE tenant_id=%s AND run_id=%s",
            (context.tenant_id, context.run_id),
        ).fetchone()

    assert saved is not None and saved["worker_budget"] == (100 if budget is None else budget)
    item = store.load_run(context).conversation.items[0]
    assert isinstance(item, Message) and item.text == "permitted input"


def test_reduced_admission_budget_stops_before_unfunded_inference(store, context, dsn):
    runtime, handlers = composition(store)
    asyncio.run(runtime.admit(context, request_id="small", text="calculate", worker_budget=1))
    step(runtime, handlers, "model_prepare")

    # Preparation consumes the one admitted worker attempt. The next job remains
    # subject to that same budget and cannot dispatch a provider call or tool.
    assert runtime.queue.claim("model_infer", handlers) is None
    assert store.load_run(context).run.status == RunStatus.LIMIT_REACHED
    assert rows(dsn, "SELECT worker_budget,worker_attempts FROM runs") == [(1, 1)]


def test_progress_attempt_generations_and_token_loss(
    store: PostgresStateStore, context: RunContext, sessions: Any
) -> None:
    runtime, handlers = fanout(store, context)
    sessions.read(context)
    sessions.progress(context, sequence=10, attempt=1, status="running", model_tokens="partial")
    sessions.progress(context, sequence=1, attempt=2, status="running", model_tokens="new")
    sessions.progress(context, sequence=99, attempt=1, status="running", model_tokens="obsolete")
    view = sessions.read(context)
    assert view["progress"][-1]["model_tokens"] == "new"
    assert len(view["progress"]) == 1
    sessions.cache.client.flushdb()
    assert sessions.read(context)["progress"] == []
    assert store.load_run(context).run.status == RunStatus.WAITING
    assert runtime.queue.claim("tool", handlers) is not None


def test_sse_stalled_reader_overflows_while_tools_finish(
    store: PostgresStateStore, context: RunContext, sessions: Any
) -> None:
    runtime, handlers = fanout(store, context)

    async def run():

        # Allow the initial reset/saved/view snapshot, then stall this reader
        # while independent tool and final events exhaust its small buffer.
        stream = ProgressStream(sessions, context, max_events=4, max_bytes=16384, poll_seconds=0.01)
        reader = stream.events()
        first = await anext(reader)
        assert first

        # A reader may briefly hold the head lock. SKIP LOCKED is allowed to
        # return no candidate; bounded polling mirrors the production worker.
        for _ in range(3):
            lease = None

            for _ in range(100):
                lease = await asyncio.to_thread(runtime.queue.claim, "tool", handlers)

                if lease is not None:
                    break

                await asyncio.sleep(0.01)

            assert lease is not None
            await AsyncWorker(runtime.queue, handlers, queue_name="tool").execute_async(lease)

        for kind in ("model_prepare", "model_infer"):
            lease = None

            for _ in range(100):
                lease = await asyncio.to_thread(runtime.queue.claim, kind, handlers)

                if lease is not None:
                    break

                await asyncio.sleep(0.01)

            assert lease is not None
            await AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(lease)

        await asyncio.sleep(0.1)
        assert stream.buffer.overflow
        assert len(stream.buffer.events) <= 4 and stream.buffer.size <= 16384
        assert store.load_run(context).run.status == RunStatus.COMPLETED
        assert b"reconnect" in await anext(reader)
        await reader.aclose()

    asyncio.run(run())


def test_tool_batch_budget_rejected_before_any_child(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    from agent_harness.coordinator.control import RunLimits

    runtime, handlers = composition(store)
    runtime.limits = RunLimits(tool_calls=2)
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    assert store.load_run(context).run.status == RunStatus.LIMIT_REACHED
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL") == [(0,)]


def test_failed_fanout_transaction_exposes_no_partial_children(
    store: PostgresStateStore, context: RunContext, dsn: str
) -> None:
    runtime, handlers = composition(store)
    asyncio.run(runtime.admit(context, request_id="request", text="calculate"))
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    completion = asyncio.run(runtime.infer_leased(context, lease.state, lease))
    bad_child = replace(completion.children[-1], resource_key="")

    with pytest.raises(ValueError):
        runtime.queue.settle(
            lease, replace(completion, children=(*completion.children[:-1], bad_child))
        )

    assert rows(dsn, "SELECT count(*) FROM tool_groups") == [(0,)]
    assert rows(
        dsn, "SELECT count(*) FROM conversation_events WHERE payload->>'type'='ToolCall'"
    ) == [(0,)]
    runtime.queue.settle(lease, completion)
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE group_id IS NOT NULL") == [(3,)]


def test_slow_tool_releases_coordinator_for_other_conversation(
    store: PostgresStateStore, context: RunContext
) -> None:
    from agent_harness.coordinator.model import ModelReply

    runtime, handlers = fanout(store, context)
    other = replace(context, conversation_id="other", run_id="other")
    store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        original = handlers.entries["tool", 1]

        async def slow_tool(c, state):
            entered.set()
            await release.wait()
            return original.execute(c, state)

        entries = dict(handlers.entries)
        entries["tool", 1] = replace(original, async_execute=slow_tool)
        lease = runtime.queue.claim("tool", Handlers(entries))
        assert lease is not None
        worker = AsyncWorker(runtime.queue, Handlers(entries), queue_name="tool")
        task = asyncio.create_task(worker.execute_async(lease))
        await asyncio.wait_for(entered.wait(), 3)

        class Answer:
            async def complete(self, request):
                return ModelReply(answer="Independent answer")

        runtime.model = Answer()
        await runtime.admit(other, request_id="other", text="answer")

        for kind in ("model_prepare", "model_infer"):
            claimed = runtime.queue.claim(kind, handlers)
            assert claimed is not None
            await AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(claimed)

        assert store.load_run(other).run.status == RunStatus.COMPLETED
        assert store.load_run(context).run.status == RunStatus.WAITING
        release.set()
        await task

    asyncio.run(run())


def test_unknown_child_denial_preserves_group_and_operation(
    store: PostgresStateStore, context: RunContext
) -> None:
    from agent_runtime.workers.reasons import FailureReason

    runtime, handlers = fanout(store, context)
    lease = runtime.queue.claim("tool", handlers)
    assert lease is not None
    runtime.queue.prepare(lease)
    runtime.queue.reject(lease, FailureReason.ACCESS_DENIED)
    controls = RecoveryControls(runtime.queue, handlers, authorize=lambda a, c, op: True)
    view = controls.inspect(RecoveryActor(context.tenant_id, "operator"), context, lease.job_id)
    assert view.unknown_effect and view.status == "stopped" and view.reason == "access_denied"
    assert view.operation_id == lease.state.operation_id
    step(runtime, handlers, "tool")
    step(runtime, handlers, "tool")
    assert store.load_run(context).run.status == RunStatus.WAITING
    assert runtime.queue.claim("model_prepare", handlers) is None
