"""Real-primary automatic tail, headroom, within-turn and overflow recovery gates."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import psycopg
import pytest

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.model import ModelContextOverflow, ModelRateLimited, ModelReply
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import PendingToolCall, VisibleResult, WaitReason
from bankeriq_agent_platform.memory.recovery import ContextRecovery
from bankeriq_agent_platform.memory.service import active_items
from bankeriq_agent_platform.storage.codec import plain
from bankeriq_agent_platform.workers.asynchronous import AsyncWorker

from .semantic_process import composition
from .test_async_groups import step
from .test_postgres import context as context
from .test_postgres import save
from .test_postgres import store as store
from .test_semantic_memory import assemble, prepare, seed
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_workers import expire, rows


def short_history(store, context, count=5):
    for number in range(count):
        ctx = replace(context, run_id=f"old-{number}")
        checkpoint = store.admit(
            ctx,
            request_id=ctx.run_id,
            items=(
                Message(
                    MessageRole.USER, f"Decision: Budget {number}. Preserve exact text {number}."
                ),
            ),
        )
        checkpoint = save(store, checkpoint, RunStatus.RUNNING)
        save(
            store,
            checkpoint,
            RunStatus.COMPLETED,
            items=(Message(MessageRole.ASSISTANT, f"Recorded {number}; no action taken."),),
        )


@pytest.mark.parametrize("keep", [0, 2])
def test_retained_tail_roles_text_and_coverage_are_exact(store, context, dsn, keep):
    short_history(store, context)
    parts = prepare(store, context, context_window=6000)
    memory = parts[2]
    memory.keep_recent_turns = keep
    policy, key = memory.policy(context)
    before = memory.storage.read(parts[5], key)
    original = tuple(
        item
        for turn in before.turns
        if turn["admission_sequence"] < before.active_turn
        for item in active_items(turn, policy)
    )
    request = assemble(parts, optional_compaction=True)
    tail = request.history[1:-1]
    assert tail == (original[-2 * keep :] if keep else ())
    assert rows(dsn, "SELECT covered_turn,partial_turn FROM semantic_summaries") == [
        (5 - keep, False)
    ]
    assert isinstance(request.history[-1], Message)
    assert request.history[-1].text == "Recall our decisions."
    assert (
        all(
            "Preserve exact text 4" not in fact["text"]
            for fact in rows(dsn, "SELECT facts FROM semantic_summaries")[0][0]
        )
        if keep
        else True
    )


def test_budget_reduces_recent_tail_before_dispatch(store, context):
    seed(store, context, 5)
    parts = prepare(store, context, context_window=1800)
    request = assemble(parts)
    assert parts[2].context_budget.fits(request)
    retained = [
        m
        for m in request.history
        if isinstance(m, Message) and m.text.startswith(" Historical synthetic detail")
    ]
    assert len(retained) < 2


@pytest.mark.parametrize("crosses", [False, True])
def test_headroom_threshold_includes_output_and_has_hysteresis(store, context, crosses):
    short_history(store, context)
    parts = prepare(store, context, context_window=6000)
    memory = parts[2]
    policy, key = memory.policy(context)
    raw = memory._request(memory.storage.read(parts[5], key), parts[6], policy)
    free = (
        memory.context_budget.window
        - memory.context_budget.count(raw)
        - memory.context_budget.output
    )
    memory.reserve_tokens = free + int(crosses)
    memory.target_free_tokens = free + 300
    first = assemble(parts)
    calls = len(parts[3].requests)
    assert bool(calls) == crosses
    second = assemble(parts)
    assert first == second
    assert len(parts[3].requests) == calls


def test_automatic_proactive_failure_can_use_checked_fitting_request(store, context):
    short_history(store, context)
    parts = prepare(store, context, context_window=6000)
    memory = parts[2]
    memory.reserve_tokens = 5000
    memory.target_free_tokens = 5500

    class Broken:
        async def complete(self, request):
            raise RuntimeError("PRIVATE_ERROR")

    memory.summarizer = Broken()
    request = assemble(parts)
    assert memory.context_budget.fits(request)
    assert "PRIVATE_ERROR" not in str(plain(request))


def workflow(store, context):
    checkpoint = store.admit(
        context,
        request_id="active",
        items=(Message(MessageRole.USER, "Decision: Preserve CHF. Run the synthetic workflow."),),
    )
    checkpoint = save(store, checkpoint, RunStatus.RUNNING)

    for number in range(4):
        checkpoint = save(
            store,
            checkpoint,
            RunStatus.RUNNING,
            items=(
                ToolCall(f"tool-{number}", "calculate", {"values": [number]}),
                VisibleResult(
                    f"tool-{number}", {"detail": "historical detail " * 200, "total": number}
                ),
            ),
        )

    return checkpoint


def prepare_workflow(store, context):
    workflow(store, context)
    runtime, handlers, memory, extractor = composition(store, context_window=3000)
    from bankeriq_agent_platform.workers.state import Recovery

    runtime.queue.attach(
        context, Recovery(kind="model_prepare", operation_id="prepare-workflow", idempotent=True)
    )
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers, seconds=120)
    assert lease is not None
    runtime.queue.prepare(lease)
    checkpoint = store.load_model_step(context, 1)
    base, _ = runtime.coordinator.prepare_request(checkpoint, assemble_history=False)
    return runtime, handlers, memory, extractor, lease, checkpoint, base


def test_long_active_turn_has_durable_prefix_and_retains_latest_tool_pair(store, context, dsn):
    parts = prepare_workflow(store, context)
    request = assemble(parts)
    assert parts[2].context_budget.fits(request)
    calls = [item for item in request.history if isinstance(item, ToolCall)]
    results = [item for item in request.history if isinstance(item, VisibleResult)]
    assert calls and calls[-1].call_id == "tool-3"
    assert [call.call_id for call in calls] == [result.call_id for result in results]
    question = "Decision: Preserve CHF. Run the synthetic workflow."
    assert sum(isinstance(item, Message) and item.text == question for item in request.history) == 1
    assert rows(dsn, "SELECT DISTINCT retained_cursor FROM semantic_summaries") == [(1,)]
    assert all(
        fact["source"] != 1
        for row in rows(dsn, "SELECT facts FROM semantic_summaries")
        for fact in row[0]
    )
    saved = rows(dsn, "SELECT covered_turn,covered_cursor,partial_turn FROM semantic_summaries")
    assert saved and all(row[0] == 1 and row[2] for row in saved)
    assert rows(dsn, "SELECT count(*) FROM conversation_events")[0][0] == 9
    count = len(parts[3].requests)
    assert assemble(parts) == request
    assert len(parts[3].requests) == count


def test_partial_turn_resumes_in_independent_worker_without_replaying_tools(store, context, dsn):
    parts = prepare_workflow(store, context)
    assemble(parts)
    expire(dsn, parts[4])
    parts[0].queue.recover_expired("model_infer")
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.semantic_process", "model_infer"],
        env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert store.load_run(context).run.status == RunStatus.COMPLETED
    assert rows(
        dsn, "SELECT count(*) FROM conversation_events WHERE payload->>'type'='VisibleResult'"
    ) == [(4,)]
    assert rows(dsn, "SELECT count(*) FROM jobs WHERE operation='tool'") == [(0,)]


@pytest.mark.parametrize("reason", [WaitReason.APPROVAL, WaitReason.JOB])
def test_partial_prefix_cannot_consume_outstanding_approval_or_job(store, context, reason):
    checkpoint = workflow(store, context)
    pending = PendingToolCall("outstanding", reason, "PRIVATE_WAIT_REFERENCE")
    checkpoint = save(
        store,
        checkpoint,
        RunStatus.WAITING,
        pending_calls=(pending,),
        items=(ToolCall("outstanding", "calculate", {"values": [42]}), pending),
    )
    _, _, memory, _ = composition(store, context_window=3000)
    policy, _ = memory.policy(context)

    with store._connect() as connection:
        events = connection.execute(
            "SELECT sequence,payload FROM conversation_events "
            "WHERE tenant_id=%s AND conversation_id=%s ORDER BY sequence",
            store._scope(context),
        ).fetchall()

    active = {"admission_sequence": 1, "events": events}

    # Prefix selection is independently callable while the owner is waiting.
    # A cut may consume settled earlier pairs, but the unresolved call and its
    # approval/job reference must remain reconstructible from the primary.
    prefix = memory._prefix(active, (), policy, "Select memory.", None, None)
    cut = prefix["events"][-1]["sequence"]
    suffix = [row for row in active["events"] if row["sequence"] > cut]
    assert [row["payload"]["type"] for row in suffix][-2:] == ["ToolCall", "PendingToolCall"]
    assert "outstanding" not in str(plain(prefix["events"]))
    assert store.load_run(context).run.pending_calls == (pending,)
    assert "PRIVATE_WAIT_REFERENCE" not in str(plain(active_items(active, policy)))


def test_cancellation_fences_partial_turn_summary_publication(store, context, dsn):
    parts = prepare_workflow(store, context)
    original = parts[3]

    class Cancelled:
        async def complete(self, request):
            reply = await original.complete(request)
            parts[0].queue.cancel(context)
            return reply

    parts[2].summarizer = Cancelled()

    with pytest.raises(StateConflictError):
        assemble(parts)

    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]
    assert rows(
        dsn, "SELECT count(*) FROM conversation_events WHERE payload->>'type'='VisibleResult'"
    ) == [(4,)]


class Overflow:
    def __init__(self, always=False):
        self.requests = []
        self.always = always

    async def complete(self, request):
        self.requests.append(request)

        if self.always or len(self.requests) == 1:
            raise ModelContextOverflow("provider_context_overflow")

        return ModelReply(answer="Recovered with sources intact.")


@pytest.mark.parametrize("always", [False, True])
def test_clean_overflow_retries_once_and_preserves_failed_attempt(store, context, dsn, always):
    short_history(store, context, 6)
    parts = prepare(store, context, context_window=6000)
    runtime, _, memory, _, lease, _, _ = parts
    model = Overflow(always)
    runtime.model = model
    completion = asyncio.run(runtime.infer_leased(context, lease.state, lease))
    runtime.queue.settle(lease, completion)
    assert len(model.requests) == 2
    assert memory.context_budget.count(model.requests[1]) < memory.context_budget.count(
        model.requests[0]
    )
    assert completion.status == (RunStatus.FAILED if always else RunStatus.COMPLETED)
    assert rows(dsn, "SELECT retry_claimed,failure_reason FROM model_context_recoveries") == [
        (
            True,
            "retry_context_overflow" if always else "provider_context_overflow",
        )
    ]
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries")[0][0] >= 1
    assert rows(dsn, "SELECT count(*) FROM conversation_events")[0][0] == 14


def test_recovery_allowance_survives_lost_worker_before_retry(store, context, dsn):
    short_history(store, context, 6)
    parts = prepare(store, context, context_window=6000)
    runtime, handlers, memory, _, lease, checkpoint, _ = parts
    request = assemble(parts)
    recovery = ContextRecovery(memory.storage)
    _, key = memory.policy(context)

    def fence(db):
        return runtime.queue._owned(db, lease)

    recovery.reject(checkpoint, key, "failed-request", 10000, fence=fence)
    recovery.claim(checkpoint, memory.context_budget.count(request), fence=fence)
    expire(dsn, lease)
    runtime.queue.recover_expired("model_infer")
    next_lease = runtime.queue.claim("model_infer", handlers, seconds=30)
    assert next_lease is not None
    runtime.queue.prepare(next_lease)
    model = Overflow()
    runtime.model = model
    result = asyncio.run(runtime.infer_leased(context, next_lease.state, next_lease))
    assert result.status == RunStatus.FAILED
    assert isinstance(result.items[0], Message)
    assert "context_recovery_exhausted" in result.items[0].text
    assert not model.requests


def test_real_rate_limit_persists_deadline_and_releases_worker(store, context, dsn):
    runtime, handlers, _, _ = composition(store)

    class Throttled:
        async def complete(self, request):
            raise ModelRateLimited(30)

    runtime.model = Throttled()
    asyncio.run(runtime.admit(context, request_id="active", text="Hello"))
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers)
    assert lease is not None
    asyncio.run(AsyncWorker(runtime.queue, handlers, queue_name="model_infer").execute_async(lease))
    assert rows(dsn, "SELECT status FROM jobs WHERE operation='model_infer'") == [("retry",)]
    assert runtime.queue.claim("model_infer", handlers) is None

    with psycopg.connect(dsn) as connection:
        result = connection.execute(
            "SELECT extract(epoch FROM due_at-clock_timestamp()) FROM jobs "
            "WHERE operation='model_infer'"
        ).fetchone()
        assert result is not None
        remaining = result[0]

    assert 25 < remaining <= 30
    assert rows(dsn, "SELECT count(*) FROM model_context_recoveries") == [(0,)]


def test_retirement_links_and_prior_versions_survive_publication_and_retrieval(store, context, dsn):
    from agent_runtime.conversations.semantic import MemoryFact
    from bankeriq_agent_platform.memory.retrieval import HistoryRetrieval

    for number, text in enumerate(("Budget: EUR 1200.", "Budget correction: CHF 1500.")):
        ctx = replace(context, run_id=f"budget-{number}")
        checkpoint = store.admit(
            ctx, request_id=ctx.run_id, items=(Message(MessageRole.USER, text),)
        )
        checkpoint = save(store, checkpoint, RunStatus.RUNNING)
        save(store, checkpoint, RunStatus.COMPLETED)

    parts = prepare(store, context, context_window=6000)
    runtime, _, memory, _, lease, checkpoint, _ = parts
    memory.keep_recent_turns = 0
    _, key = memory.policy(context)
    window = memory.storage.read(checkpoint, key)

    def fence(db):
        return runtime.queue._owned(db, lease)

    operation = memory.storage.reserve(
        window,
        target_turn=1,
        target_cursor=1,
        input_hash="prior",
        prompt=memory.prompts.resolve(),
        fence=fence,
    )
    memory.storage.publish(
        window, operation, (MemoryFact(1, 1, "user", "Budget: EUR 1200.", "decision"),), fence=fence
    )

    class Corrector:
        async def complete(self, request):
            sources = json.loads(request.history[0].text.split("\n", 1)[1])
            old = next(f["candidate"] for f in sources if f["label"] == "decision")
            new = next(f["candidate"] for f in sources if "correction" in f["text"])
            return ModelReply(
                answer=json.dumps(
                    {
                        "entries": [{"candidate": new, "label": "decision"}],
                        "retirements": [
                            {"previous": old, "replacement": new, "relation": "supersedes"}
                        ],
                    }
                )
            )

    memory.summarizer = Corrector()
    request = assemble(parts, optional_compaction=True)
    assert "CHF 1500" in str(plain(request)) and "EUR 1200" not in str(plain(request))
    versions = rows(dsn, "SELECT facts,retirements FROM semantic_summaries ORDER BY version")
    assert len(versions) == 2
    assert versions[0][0][0]["text"] == "Budget: EUR 1200."
    assert versions[1][1][0]["previous"]["source"] == 1
    retrieved = HistoryRetrieval(memory).execute(
        context, ToolCall("history", "retrieve_history", {"after": 0, "limit": 1}), "history"
    )
    assert "EUR 1200" in str(retrieved.payload)
