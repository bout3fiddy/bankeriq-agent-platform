"""Real primary/cache evidence for provisional text, cancellation and durable final recovery."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace

import psycopg
import pytest

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.model import ModelReply, ModelRequest
from agent_harness.runs.state import RunStatus
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.sessions.publication import TextPublication
from agent_runtime.sessions.stream import ProgressStream
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.state import Completion, Recovery

from .async_process import composition
from .test_async_groups import step
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import expire, wait_file


def prepared(store, context):
    runtime, handlers = composition(store)
    asyncio.run(runtime.admit(context, request_id="request", text="synthetic prompt"))
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers, seconds=10)
    assert lease is not None
    return runtime, handlers, lease


def drain(stream):
    frames = []

    while (frame := stream.buffer.take()) is not None:
        frames.append(frame)

    return b"".join(frames)


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize("overflowed", [False, True])
def test_failed_poller_wakes_reader_and_discards_unsent_payloads(
    sessions, context, monkeypatch, caplog, revoked, overflowed
):
    caplog.set_level(logging.INFO, logger="bankeriq.lifecycle")
    stream = ProgressStream(sessions, context, heartbeat_seconds=60)

    def failed_poll(**kwargs):
        stream.buffer.offer("text", {"text": "UNSENT_PRIVATE_FIXTURE"})

        if overflowed:
            stream.buffer.offer("text", {"text": "x" * stream.buffer.max_bytes})
            assert stream.buffer.overflow

        if revoked:
            raise StateConflictError("PRIVATE_SCOPE_DETAILS")

        raise RuntimeError("PRIVATE_ADAPTER_DETAILS")

    monkeypatch.setattr(stream, "poll", failed_poll)

    async def receive():
        return [frame async for frame in stream.events()]

    async def bounded():
        return await asyncio.wait_for(receive(), 1)

    frames = asyncio.run(bounded())
    reason = "reauthorize" if revoked else "backend_unavailable"
    assert frames == [f'event: reconnect\ndata: {{"reason": "{reason}"}}\n\n'.encode()]
    assert "PRIVATE" not in caplog.text
    assert ("stream.failed" in caplog.text) != revoked


def test_streaming_worker_publishes_text_then_one_durable_final(store, context, sessions):
    runtime, handlers, lease = prepared(store, context)
    observed = []

    class Model:
        async def stream(self, request, on_text):
            for text in ["The total ", "is five."]:
                await on_text(text)

                for _ in range(100):
                    snapshot = await asyncio.to_thread(sessions.stream_snapshot, context)

                    if snapshot["text"] and text in snapshot["text"]["text"]:
                        observed.append(snapshot)
                        break

                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("provisional text never became visible")

            return ModelReply(answer="The total is five.")

    runtime.streaming = Model()
    runtime.publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
    asyncio.run(AsyncWorker(runtime.queue, handlers, queue_name="model_infer").execute_async(lease))
    assert [entry["text"]["text"] for entry in observed] == ["The total ", "The total is five."]
    assert all(entry["final"] is None for entry in observed)
    assert store.load_run(context).run.status == RunStatus.COMPLETED
    stream = ProgressStream(sessions, context)
    stream.poll()
    first = drain(stream)
    stream.poll()
    assert first.count(b"event: final") == 1
    assert b"event: final" not in drain(stream)

    # A new API process can recover the final after complete Redis loss. Its
    # client uses run identity to replace a draft, avoiding duplicate messages.
    sessions.cache.client.flushdb()
    other = ProgressStream(sessions, context, cursor=stream.cursor)
    other.poll()
    assert b"The total is five." in drain(other)
    assert sessions.stream_snapshot(context)["final"]["terminal_written_at"] is not None


def test_cancelled_or_expired_owner_cannot_deliver_stale_text(store, context, sessions, dsn):
    runtime, handlers, lease = prepared(store, context)
    publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
    publication.write(lease, 1, "old attempt")
    assert sessions.stream_snapshot(context)["text"]["text"] == "old attempt"
    runtime.queue.prepare(lease)
    expire(dsn, lease)
    replacement = runtime.queue.claim("model_infer", handlers, seconds=10)
    assert replacement is not None
    assert sessions.stream_snapshot(context)["text"] is None

    with pytest.raises(StateConflictError):
        publication.write(lease, 99, "obsolete")

    publication.write(replacement, 1, "new attempt")
    assert sessions.stream_snapshot(context)["text"]["text"] == "new attempt"
    runtime.queue.cancel(context)
    final = sessions.stream_snapshot(context)
    assert final["text"] is None
    assert final["final"]["status"] == "cancelled"

    with pytest.raises(StateConflictError):
        publication.write(replacement, 2, "late cancellation text")


def test_process_restart_replaces_interrupted_draft_and_fresh_api_recovers_final(
    store, context, sessions, dsn, redis_url, tmp_path
):
    runtime, handlers = composition(store)
    asyncio.run(runtime.admit(context, request_id="stream-restart", text="synthetic input"))
    step(runtime, handlers, "model_prepare")
    environment = {
        **os.environ,
        "BANKERIQ_TEST_POSTGRES_DSN": dsn,
        "BANKERIQ_TEST_REDIS_URL": redis_url,
    }
    command = [sys.executable, "-m", "tests.integration.stream_process"]
    encoded_context = json.dumps(asdict(context))
    children = []

    try:
        old = tmp_path / "old"
        child = subprocess.Popen(
            command + ["crash", str(old), encoded_context], env=environment, text=True
        )
        children.append(child)
        wait_file(old, child)
        first = sessions.stream_snapshot(context)["text"]
        assert first["text"] == "Interrupted draft"
        child.kill()
        child.wait(timeout=5)

        # Simulate elapsed lease time after real process death. The old Redis
        # draft is still present, but primary ownership now prevents delivery.
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second' "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
                (context.tenant_id, context.conversation_id, first["job_id"]),
            )

        assert sessions.stream_snapshot(context)["text"] is None
        resumed = tmp_path / "resumed"
        child = subprocess.Popen(
            command + ["resume", str(resumed), encoded_context], env=environment, text=True
        )
        children.append(child)
        wait_file(resumed, child)
        second = sessions.stream_snapshot(context)["text"]
        assert second["text"] == "Recovered answer"
        assert int(second["attempt"]) > int(first["attempt"])
        resumed.with_suffix(".finish").touch()
        assert child.wait(timeout=10) == 0

        # Remove only this fixture's cache keys. A new API process reconstructs
        # the final from PostgreSQL and never replays the interrupted draft.
        head = sessions.history.head(context)
        keys = list(sessions.cache.client.scan_iter(match=sessions.cache.key(head) + "*"))

        if keys:
            sessions.cache.client.delete(*keys)

        result = subprocess.run(
            command + ["read", str(tmp_path / "api"), encoded_context],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        frames = "".join(json.loads(result.stdout))
        assert frames.count("event: final") == 1
        assert "Recovered answer" in frames and "Interrupted draft" not in frames
        checkpoint = store.load_run(context)
        assert checkpoint.run.status == RunStatus.COMPLETED
        assert [
            item.text
            for item in checkpoint.conversation.items
            if isinstance(item, Message) and item.role == MessageRole.ASSISTANT
        ] == ["Recovered answer"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_policy_scope_and_byte_bounds_precede_publication(store, context, sessions):
    runtime, _, lease = prepared(store, context)
    denied = TextPublication(sessions, runtime.queue, allow=lambda _: False)

    with pytest.raises(StateConflictError, match="denied"):
        denied.write(lease, 1, "must stay unpublished")

    assert sessions.stream_snapshot(context)["text"] is None
    allowed = TextPublication(sessions, runtime.queue, allow=lambda _: True)

    with pytest.raises(ValueError, match="invalid"):
        allowed.write(lease, 1, "界" * 12000)

    allowed.write(lease, 2, "permitted")
    allowed.write(lease, 1, "older sequence")
    assert sessions.stream_snapshot(context)["text"]["text"] == "permitted"

    with pytest.raises(StateNotFoundError):
        sessions.stream_snapshot(replace(context, user_id="other"))

    with pytest.raises(StateNotFoundError):
        sessions.stream_snapshot(replace(context, tenant_id="other"))


def test_snapshot_does_not_wait_for_uncommitted_conversation_changes(
    store, context, sessions, monkeypatch
):
    prepared(store, context)
    connect = store._connect

    @contextmanager
    def bounded_connection():
        with connect() as connection:
            connection.execute("SET LOCAL lock_timeout='200ms'")
            yield connection

    # An active writer may be saving a terminal result while an SSE reader polls.
    # The read must finish from committed rows without holding up either side.
    # After the writer commits a tombstone, the very next read loses access.
    with store._connect() as writer:
        writer.execute(
            "UPDATE conversations SET deleted=true WHERE tenant_id=%s AND conversation_id=%s",
            store._scope(context),
        )

        monkeypatch.setattr(store, "_connect", bounded_connection)
        assert sessions.stream_snapshot(context)["status"] == "running"

    with pytest.raises(StateNotFoundError):
        sessions.stream_snapshot(context)


def test_snapshot_rejects_a_replaced_database_generation(store, context, sessions):
    prepared(store, context)

    with store._connect() as connection:
        connection.execute("UPDATE runtime_generation SET recovery_id='new-stream-timeline'")

    with pytest.raises(StateConflictError, match="generation"):
        sessions.stream_snapshot(context)


def test_redis_loss_emits_reset_and_does_not_cancel_the_model(store, context, sessions):
    runtime, _, lease = prepared(store, context)
    publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
    publication.write(lease, 1, "draft")
    stream = ProgressStream(sessions, context)
    stream.poll()
    assert b"event: text" in drain(stream)
    sessions.cache.client.flushdb()
    stream.poll()
    assert b"event: reset" in drain(stream)
    assert store.load_run(context).run.status == RunStatus.RUNNING


def test_slow_publication_coalesces_without_stalling_model(store, context, sessions):
    runtime, _, lease = prepared(store, context)
    calls = []

    class Model:
        async def stream(self, request, on_text):
            for _ in range(1000):
                await on_text("x")

            return ModelReply(answer="x" * 1000)

    publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
    publication.write = lambda lease, sequence, text, kind="text": calls.append(
        (lease, sequence, text, kind)
    )
    reply = asyncio.run(publication.complete(Model(), ModelRequest((), ()), lease))
    assert reply.answer == "x" * 1000
    assert len(calls) <= 1


def test_text_ttl_is_not_renewed_by_stream_or_inspection(store, context, sessions):
    runtime, _, lease = prepared(store, context)
    publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
    publication.write(lease, 1, "draft")
    prefix = sessions.cache.key(sessions.history.head(context))
    keys = list(sessions.cache.client.scan_iter(match=prefix + ":text:*"))
    assert len(keys) == 1
    sessions.cache.client.pexpire(keys[0], 2000)
    before = sessions.cache.client.pttl(keys[0])
    stream = ProgressStream(sessions, context)

    for _ in range(3):
        stream.poll()
        drain(stream)
        sessions.inspect(context)

    assert 0 < sessions.cache.client.pttl(keys[0]) <= before


def test_provisional_progress_never_exposes_an_unbounded_payload(store, context, sessions):
    prepared(store, context)

    with pytest.raises(ValueError):
        sessions.progress(context, sequence=1, status="running", model_tokens="x" * 8193)

    assert len(json.dumps(sessions.progress_view(context)).encode()) < 10000


def test_cancelled_tool_preamble_is_not_presented_as_a_final_answer(store, context, sessions):
    runtime, _, lease = prepared(store, context)
    runtime.queue.prepare(lease)
    runtime.queue.settle(
        lease,
        Completion(
            RunStatus.RUNNING,
            items=(Message(MessageRole.ASSISTANT, "I will calculate that."),),
            next_task=Recovery(
                kind="model_prepare",
                operation_id="next",
                idempotent=True,
                run_budget=lease.state.run_budget,
            ),
        ),
    )
    runtime.queue.cancel(context)
    assert sessions.stream_snapshot(context)["final"]["answer"] is None


def test_idle_transport_heartbeat_does_not_mark_conversation_activity(store, context, sessions):
    prepared(store, context)

    with store._connect() as connection:
        connection.execute(
            "UPDATE conversations SET last_activity=clock_timestamp()-interval '60 seconds' "
            "WHERE tenant_id=%s AND conversation_id=%s",
            store._scope(context),
        )

    before = sessions.inspect(context)
    activity_age = sessions.history.head(context)["idle_seconds"]

    async def observe():
        stream = ProgressStream(sessions, context, heartbeat_seconds=0.01)
        reader = stream.events()

        try:
            async with asyncio.timeout(2):
                while True:
                    if await anext(reader) == b": keepalive\n\n":
                        return
        finally:
            await reader.aclose()

    asyncio.run(observe())
    after = sessions.inspect(context)
    assert before["durable"] == after["durable"]
    assert before["cursor"] == after["cursor"]
    assert sessions.history.head(context)["idle_seconds"] >= activity_age >= 60
