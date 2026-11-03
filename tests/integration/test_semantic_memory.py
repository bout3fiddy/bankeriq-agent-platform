"""Real-primary semantic memory, visibility, failure and restart acceptance gates."""

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.conversations.semantic import MemoryBlocked
from agent_runtime.coordinator.model import ModelReply
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import ToolFailure, VisibleResult
from bankeriq_agent_platform.memory.retrieval import HistoryRetrieval
from bankeriq_agent_platform.memory.service import HistoryPolicy, project
from bankeriq_agent_platform.storage.codec import plain
from bankeriq_agent_platform.storage.postgres import PostgresStateStore

from .semantic_process import Answer, composition
from .test_async_groups import step
from .test_postgres import context as context
from .test_postgres import save
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url
from .test_sessions import sessions as sessions
from .test_workers import expire, rows


def seed(store, context, count=12):
    for number in range(count):
        ctx = replace(context, run_id=f"past-{number}")
        key = "Decision: Use EUR. Open question: Which deadline?." if number == 0 else ""

        if number == 1:
            key = "Decision: Use CHF instead of EUR."

        text = key + " Historical synthetic detail " * 150
        checkpoint = store.admit(
            ctx, request_id=f"past-{number}", items=(Message(MessageRole.USER, text),)
        )
        checkpoint = save(store, checkpoint, RunStatus.RUNNING)
        save(
            store,
            checkpoint,
            RunStatus.COMPLETED,
            items=(Message(MessageRole.ASSISTANT, "Acknowledged; no action performed."),),
        )


def prepare(store, context, *, context_window=3000):
    runtime, handlers, memory, extractor = composition(store, context_window=context_window)
    asyncio.run(runtime.admit(context, request_id="active", text="Recall our decisions."))
    step(runtime, handlers, "model_prepare")
    lease = runtime.queue.claim("model_infer", handlers, seconds=120)
    assert lease is not None
    runtime.queue.prepare(lease)
    checkpoint = store.load_model_step(context, 1)
    base, _ = runtime.coordinator.prepare_request(checkpoint, assemble_history=False)
    return runtime, handlers, memory, extractor, lease, checkpoint, base


def assemble(parts, **kwargs):
    runtime, _, memory, _, lease, checkpoint, base = parts
    return asyncio.run(
        memory.assemble(
            checkpoint, base, fence=lambda db: runtime.queue._owned(db, lease), **kwargs
        )
    )


def test_long_memory_restart_redis_loss_and_source_retrieval(store, context, dsn, sessions):
    seed(store, context, 24)
    parts = prepare(store, context)
    request = assemble(parts)
    memory = parts[2]
    assert isinstance(request.history[0], Message)
    text = request.history[0].text
    assert "Use EUR" in text and "Use CHF instead of EUR" in text
    assert "Which deadline?" in text
    assert memory.context_budget.fits(request)
    assert len(parts[3].requests) > 1
    assert all(memory.summary_budget.fits(r) for r in parts[3].requests)
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries")[0][0] > 1
    sessions.read(context)
    sessions.cache.client.delete(sessions.cache.key(sessions.history.head(context)))

    # A new interpreter reconstructs summaries solely from PostgreSQL. Release
    # the first owned claim and let the worker resume the same prepared step.
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
    assert "Remembered CHF" in sessions.read(context)["events"][-1]["payload"]["value"]["text"]


def test_scoped_retrieval_duplicate_limits_and_gateway_revocation(store, context, dsn):
    seed(store, context)
    parts = prepare(store, context)
    assemble(parts)
    runtime, _, memory, _, _, _, _ = parts
    retrieval = HistoryRetrieval(memory)
    call = ToolCall("lookup-1", "retrieve_history", {"after": 0, "limit": 2})
    first = retrieval.execute(context, call, "retrieve-1")
    assert first == retrieval.execute(context, call, "retrieve-1")
    assert "Historical synthetic detail" in str(first.payload)

    for index in range(2, 5):
        retrieval.execute(context, call, f"retrieve-{index}")

    with pytest.raises(MemoryBlocked, match="retrieval_limit"):
        retrieval.execute(context, call, "retrieve-5")

    assert rows(dsn, "SELECT count(*) FROM semantic_retrievals") == [(4,)]

    for changed in (replace(context, tenant_id="other"), replace(context, user_id="other")):
        with pytest.raises(StateNotFoundError):
            retrieval.execute(changed, call, "cross-scope")

    bad = ToolCall("bad", "retrieve_history", {"after": 0, "limit": 1, "tenant_id": "other"})
    assert isinstance(
        runtime.gateway.dispatch(bad, version="1", context=context).model_result, ToolFailure
    )
    runtime.gateway._access.permissions_for = lambda context: frozenset()
    assert isinstance(
        runtime.gateway.dispatch(call, version="1", context=context).model_result, ToolFailure
    )


def test_queued_input_is_excluded_even_when_its_cursor_precedes_a_completion(store, context):
    first = replace(context, run_id="first")
    checkpoint = store.admit(
        first, request_id="first", items=(Message(MessageRole.USER, "Decision: Use EUR."),)
    )
    checkpoint = save(store, checkpoint, RunStatus.RUNNING)
    store.admit(
        context, request_id="active", items=(Message(MessageRole.USER, "Recall our decisions."),)
    )
    save(store, checkpoint, RunStatus.COMPLETED, items=(Message(MessageRole.ASSISTANT, "Done"),))
    parts = prepare(store, context)
    later = replace(context, run_id="later")
    store.admit(later, request_id="later", items=(Message(MessageRole.USER, "FUTURE_SECRET"),))
    request = assemble(parts, optional_compaction=True)
    assert "FUTURE_SECRET" not in str(plain(request))
    assert "Recall our decisions." in str(plain(request))
    result = HistoryRetrieval(parts[2]).execute(
        context, ToolCall("r", "retrieve_history", {"after": 0, "limit": 20}), "r"
    )
    assert "FUTURE_SECRET" not in str(result.payload)


@pytest.mark.parametrize("failure", ["provider", "invented", "huge", "instructions", "tools"])
def test_required_compaction_blocks_without_model_dispatch(store, context, dsn, failure):
    seed(store, context)
    runtime, handlers, memory, _ = composition(store)

    class Broken:
        async def complete(self, request):
            if failure == "provider":
                raise RuntimeError("PRIVATE_ERROR_MARKER")

            return ModelReply(
                answer="x" * 40000
                if failure == "huge"
                else '{"entries":[{"source":999,"quote":"done","label":"fact"}]}'
            )

    memory.summarizer = Broken()

    if failure == "instructions":
        runtime.coordinator._prompts = runtime.queue.prompts = FixedPromptResolver("big " * 4000)

    if failure == "tools":
        registry = runtime.coordinator._registry
        tool = registry.discover(frozenset({"history.read"}))[0]
        from agent_runtime.tools.registry import ToolRegistry

        runtime.coordinator._registry = ToolRegistry((replace(tool, description="large " * 4000),))

    asyncio.run(runtime.admit(context, request_id="active", text="Recall"))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    assert store.load_run(context).run.status == RunStatus.FAILED
    assert isinstance(runtime.model, Answer) and not runtime.model.requests
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]
    assert "PRIVATE_ERROR_MARKER" not in str(plain(store.load_run(context)))
    assert "Conversation memory unavailable" in str(plain(store.load_run(context)))


def test_optional_failure_preserves_fitting_context(store, context, dsn):
    seed(store, context, 2)
    parts = prepare(store, context, context_window=10000)

    class Broken:
        async def complete(self, request):
            raise RuntimeError("synthetic failure")

    parts[2].summarizer = Broken()
    request = assemble(parts, optional_compaction=True)
    assert isinstance(request.history[0], Message)
    assert "Historical synthetic detail" in request.history[0].text
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]


@pytest.mark.parametrize("change", ["delete", "restore", "cancel", "lease", "access"])
def test_publication_rejects_changed_execution_scope(store, context, dsn, change):
    seed(store, context, 3)
    parts = prepare(store, context, context_window=1800)
    runtime, _, memory, extractor, lease, _, _ = parts

    class Racing:
        async def complete(self, request):
            reply = await extractor.complete(request)

            if change == "delete":
                store.delete_conversation(context)
            elif change == "restore":
                with psycopg.connect(dsn) as connection:
                    connection.execute("UPDATE runtime_generation SET recovery_id='restored'")
            elif change == "cancel":
                runtime.queue.cancel(context)
            elif change == "lease":
                with psycopg.connect(dsn) as connection:
                    connection.execute(
                        "UPDATE jobs SET lease_expiry=now()-interval '1 second' WHERE job_id=%s",
                        (lease.job_id,),
                    )
            else:
                memory.access_for = lambda c: HistoryPolicy("revoked", frozenset(), lambda f: False)

            return reply

    memory.summarizer = Racing()

    with pytest.raises((StateConflictError, StateNotFoundError, MemoryBlocked)):
        assemble(parts)

    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]


def test_restricted_content_reasoning_and_delivery_ids_never_become_sources():
    turn = {
        "admission_sequence": 1,
        "events": [
            {
                "sequence": 1,
                "payload": {
                    "type": "ToolCall",
                    "value": {
                        "call_id": "c",
                        "tool_name": "report",
                        "arguments": {"ref": "PRIVATE_REF"},
                    },
                },
            },
            {
                "sequence": 2,
                "payload": {
                    "type": "RestrictedReceipt",
                    "value": {"call_id": "c", "delivery": "PRIVATE_DELIVERY"},
                },
            },
            {"sequence": 3, "payload": {"type": "Reasoning", "value": {"text": "PRIVATE_REASON"}}},
            {
                "sequence": 4,
                "payload": {
                    "type": "PendingToolCall",
                    "value": {
                        "call_id": "c",
                        "reason": "approval",
                        "reference_id": "PRIVATE_APPROVAL",
                    },
                },
            },
        ],
    }
    facts = project(turn, HistoryPolicy("1", frozenset(), lambda f: True))
    assert "PRIVATE" not in str(plain(facts))
    assert [f.kind for f in facts] == ["tool_proposed", "tool_completed", "tool_pending"]


def test_publication_duplicate_conflict_stale_attempt_and_rollback(store, context, dsn):
    from agent_runtime.conversations.semantic import MemoryFact

    from .test_postgres import Fault

    seed(store, context, 3)
    runtime, _, memory, _, lease, checkpoint, _ = prepare(store, context)
    storage = memory.storage
    window = storage.read(checkpoint, memory.policy(context)[1])

    def fence(db):
        return runtime.queue._owned(db, lease)

    options: dict[str, Any] = dict(
        target_turn=1,
        target_cursor=2,
        input_hash="synthetic-input",
        prompt=memory.prompts.resolve(),
        fence=fence,
    )
    old = storage.reserve(window, **options)
    new = storage.reserve(window, **options)
    competing = storage.reserve(window, **{**options, "target_turn": 2, "target_cursor": 5})
    facts = (MemoryFact(1, 1, "user", "Decision: Use EUR.", "decision"),)

    with pytest.raises(StateConflictError, match="attempt"):
        storage.publish(window, old, facts, fence=fence)

    with pytest.raises(MemoryBlocked, match="attempts_exhausted"):
        storage.reserve(window, **options)

    storage.store = PostgresStateStore(dsn, fault=Fault(write=2))

    with pytest.raises(RuntimeError, match="rollback"):
        storage.publish(window, new, facts, fence=fence)

    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]
    storage.store = PostgresStateStore(dsn, fault=Fault(lost_ack=True))
    assert storage.publish(window, new, facts, fence=fence) == 1
    assert storage.publish(window, new, facts, fence=fence) == 1

    with pytest.raises(StateConflictError, match="version"):
        storage.publish(window, competing, facts, fence=fence)

    with pytest.raises(StateConflictError, match="conflicting"):
        storage.publish(window, new, (), fence=fence)

    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(1,)]
    assert rows(dsn, "SELECT count(*) FROM semantic_operations WHERE result_version=1") == [(1,)]


def test_retry_uses_pinned_summary_prompt_and_budget(store, context, dsn):
    seed(store, context, 4)
    parts = prepare(store, context, context_window=1800)
    memory = parts[2]
    versions = [FixedPromptResolver("summary-v1"), FixedPromptResolver("summary-v2 " * 100)]

    class Prompts:
        active = 0

        def resolve(self):
            return versions[self.active].resolve()

        def instructions(self, manifest):
            return next(p.instructions(manifest) for p in versions if p.resolve() == manifest)

    prompts = Prompts()
    memory.prompts = prompts
    requests = []

    class Broken:
        async def complete(self, request):
            requests.append(request)
            raise RuntimeError("synthetic failure")

    memory.summarizer = Broken()

    for _ in range(2):
        with pytest.raises(MemoryBlocked, match="provider_failed"):
            assemble(parts)

        prompts.active = 1

    with pytest.raises(MemoryBlocked, match="attempts_exhausted"):
        assemble(parts)

    assert len(requests) == 2
    assert requests[0].prompt_manifest == requests[1].prompt_manifest == versions[0].resolve()
    assert requests[0].instructions == requests[1].instructions
    assert rows(dsn, "SELECT attempts FROM semantic_operations") == [(2,)]


def test_source_token_limit_stops_before_summarizer(store, context, dsn):
    seed(store, context, 3)
    parts = prepare(store, context, context_window=1800)
    parts[2].summary_budget = replace(parts[2].summary_budget, window=1200, output=512)

    with pytest.raises(MemoryBlocked, match="source_token_limit"):
        assemble(parts)

    assert not parts[3].requests
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]


def test_new_policy_rebuilds_memory_and_old_retrieval_cannot_bypass_it(store, context):
    seed(store, context, 6)
    parts = prepare(store, context, context_window=2400)
    assert "Use EUR" in str(plain(assemble(parts)))
    retrieval = HistoryRetrieval(parts[2])
    call = ToolCall("r", "retrieve_history", {"after": 0, "limit": 1})
    assert "Use EUR" in str(retrieval.execute(context, call, "r").payload)
    parts[2].access_for = lambda c: HistoryPolicy("policy-2", frozenset(), lambda f: f.turn > 2)

    with pytest.raises(StateConflictError):
        retrieval.execute(context, call, "r")

    assert "Use EUR" not in str(plain(assemble(parts)))


def test_retrieval_through_model_gateway_and_one_continuation(store, context, dsn):
    seed(store, context, 8)
    runtime, handlers, _, _ = composition(store)

    class RetrieveThenAnswer:
        async def complete(self, request):
            results = [item for item in request.history if isinstance(item, VisibleResult)]

            if not results:
                return ModelReply(
                    calls=(ToolCall("retrieve", "retrieve_history", {"after": 0, "limit": 1}),)
                )

            assert "Historical synthetic detail" in str(results[0].payload)
            return ModelReply(answer="Recovered the original detail from source cursor 1.")

    runtime.model = RetrieveThenAnswer()
    asyncio.run(runtime.admit(context, request_id="active", text="Retrieve source 1."))

    for kind in ("model_prepare", "model_infer", "tool", "model_prepare", "model_infer"):
        step(runtime, handlers, kind)

    assert store.load_run(context).run.status == RunStatus.COMPLETED
    assert store.load_run(context).run.tool_calls == 1
    assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]
    assert rows(dsn, "SELECT count(*) FROM semantic_retrievals") == [(1,)]


def test_killed_summary_worker_resumes_with_bounded_attempts(store, context, dsn, tmp_path):
    from .test_workers import wait_file

    seed(store, context, 12)
    runtime, handlers, _, _ = composition(store)
    asyncio.run(runtime.admit(context, request_id="active", text="Recall decisions."))
    step(runtime, handlers, "model_prepare")
    marker = tmp_path / "summary-dispatched"
    command = [sys.executable, "-m", "tests.integration.semantic_process", "model_infer"]
    first = subprocess.Popen(
        command,
        env={
            **os.environ,
            "BANKERIQ_TEST_POSTGRES_DSN": dsn,
            "SEMANTIC_DISPATCH_MARKER": str(marker),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_file(marker, first)
        first.kill()
        first.communicate(timeout=5)
        assert first.returncode != 0
        assert rows(dsn, "SELECT count(*) FROM semantic_summaries") == [(0,)]

        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE jobs SET lease_expiry=now()-interval '1 second' "
                "WHERE operation='model_infer'"
            )

        runtime.queue.recover_expired("model_infer")
        second = subprocess.run(
            command,
            env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert second.returncode == 0, second.stderr
        assert store.load_run(context).run.status == RunStatus.COMPLETED
        assert rows(dsn, "SELECT max(attempts) FROM semantic_operations") == [(2,)]
        assert rows(
            dsn, "SELECT count(*) FROM semantic_operations WHERE result_version IS NULL"
        ) == [(0,)]
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)


def test_summarizer_local_provider_transport_honors_reserved_output(store, context):
    import json
    import re

    import httpx2
    from openai import AsyncOpenAI

    from agent_runtime.providers.foundry.model import AsyncFoundryModelClient

    seed(store, context, 12)
    parts = prepare(store, context)
    payloads = []

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        sources = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        entries = [
            {
                "candidate": fact["candidate"],
                "label": "open_question" if "question" in quote else "decision",
            }
            for fact in sources
            for quote in re.findall(r"(?:Decision|Open question): [^.]+\.", fact["text"])
        ]
        return httpx2.Response(
            200,
            json={
                "id": "synthetic-summary",
                "object": "chat.completion",
                "created": 1,
                "model": "synthetic-gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"entries": entries}),
                        },
                    }
                ],
            },
        )

    async def exercise():
        async with AsyncOpenAI(
            api_key="synthetic",
            base_url="https://synthetic.invalid/v1/",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            parts[2].summarizer = AsyncFoundryModelClient(
                client, deployment="summary", max_completion_tokens=4096
            )
            return await parts[2].assemble(
                parts[5], parts[6], fence=lambda db: parts[0].queue._owned(db, parts[4])
            )

    request = asyncio.run(exercise())
    assert payloads and all(p["max_completion_tokens"] == 512 for p in payloads)
    assert all(p["stream"] is False and p["store"] is False for p in payloads)
    assert all("tools" not in p for p in payloads)
    assert all(p["response_format"]["type"] == "json_schema" for p in payloads)
    assert all(p["response_format"]["json_schema"]["strict"] is True for p in payloads)
    assert all(
        p["response_format"]["json_schema"]["schema"]["required"] == ["entries", "retirements"]
        for p in payloads
    )
    assert "Use EUR" in str(plain(request))


def test_new_input_during_compaction_remains_outside_fixed_coverage(store, context):
    seed(store, context, 6)
    parts = prepare(store, context, context_window=2400)
    runtime, _, memory, extractor, _, _, _ = parts

    class ConcurrentInput:
        async def complete(self, request):
            later = replace(context, run_id="later")
            await runtime.admit(later, request_id="later", text="LATER_PRIVATE_MARKER")
            return await extractor.complete(request)

    memory.summarizer = ConcurrentInput()
    request = assemble(parts)
    assert "LATER_PRIVATE_MARKER" not in str(plain(request))
    assert all("LATER_PRIVATE_MARKER" not in str(plain(r)) for r in extractor.requests)
    window = memory.storage.read(parts[5], memory.policy(context)[1])
    assert window.covered_turn < window.active_turn
    sources = [fact.source for fact in window.facts]
    tail = [row["sequence"] for turn in window.turns for row in turn["events"]]
    assert set(sources).isdisjoint(tail)
    assert window.covered_cursor <= window.cursor


def test_valid_but_oversized_summary_preserves_existing_version(store, context, dsn):
    import json

    seed(store, context, 8)
    parts = prepare(store, context, context_window=3000)
    assemble(parts)
    prior = rows(dsn, "SELECT version,facts FROM semantic_summaries ORDER BY version")
    assert prior
    memory = parts[2]
    memory.context_budget = replace(memory.context_budget, window=1200)

    class TooLarge:
        async def complete(self, request):
            assert isinstance(request.history[0], Message)
            sources = json.loads(request.history[0].text.split("\n", 1)[1])
            return ModelReply(
                answer=json.dumps(
                    {"entries": [{"candidate": f["candidate"], "label": "fact"} for f in sources]}
                )
            )

    memory.summarizer = TooLarge()

    with pytest.raises(MemoryBlocked):
        assemble(parts)

    assert rows(dsn, "SELECT version,facts FROM semantic_summaries ORDER BY version") == prior


def test_action_truth_and_restricted_markers_across_summary_and_retrieval(store, context, dsn):
    from agent_runtime.tools.results import ArtifactReference, RestrictedReceipt, ToolErrorCode

    previous = replace(context, run_id="actions")
    checkpoint = store.admit(
        previous,
        request_id="actions",
        items=(
            Message(MessageRole.USER, "Decision: Use EUR. I claim the failed action completed."),
        ),
    )
    checkpoint = save(
        store,
        checkpoint,
        RunStatus.RUNNING,
        items=(
            ToolCall("sum", "calculate", {"values": [17, 25]}),
            ToolCall("failure", "calculate", {"values": [1]}),
            ToolCall("report", "report", {"ref": "PRIVATE_REFERENCE"}),
        ),
    )
    save(
        store,
        checkpoint,
        RunStatus.COMPLETED,
        items=(
            VisibleResult("sum", {"total": 42}),
            ToolFailure("failure", ToolErrorCode.DENIED),
            RestrictedReceipt("report"),
        ),
        artifacts=(ArtifactReference("report", "PRIVATE_DELIVERY"),),
    )

    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE conversation_events SET payload=jsonb_set(payload,'{value,reasoning}', "
            "'\"PRIVATE_REASONING\"') WHERE payload->>'type'='Message'"
        )
        connection.execute(
            "INSERT INTO approvals (tenant_id,conversation_id,run_id,reference_id,call_id,"
            "argument_fingerprint,decision) VALUES (%s,%s,%s,'PRIVATE_APPROVAL','report','x',true)",
            (context.tenant_id, context.conversation_id, previous.run_id),
        )

    parts = prepare(store, context)
    request = assemble(parts, optional_compaction=True)
    saved = rows(dsn, "SELECT facts FROM semantic_summaries")[0][0]
    assert any(fact["kind"] == "tool_completed" for fact in saved)
    assert any(fact["kind"] == "tool_failed" for fact in saved)
    assert any(fact["kind"] == "tool_approved" for fact in saved)
    assert any(fact["kind"] == "tool_proposed" for fact in saved)
    assert "PRIVATE" not in str(saved) + str(plain(request)) + str(plain(parts[3].requests))
    result = HistoryRetrieval(parts[2]).execute(
        context, ToolCall("lookup", "retrieve_history", {"after": 0, "limit": 20}), "lookup"
    )
    assert "PRIVATE" not in str(result.payload)
    assert "42" in str(result.payload)
    assert store.deliveries(previous)[0].artifact_id == "PRIVATE_DELIVERY"


def test_existing_context_byte_limit_still_blocks_semantic_dispatch(store, context):
    seed(store, context, 3)
    runtime, handlers, _, _ = composition(store)
    runtime.limits = replace(runtime.limits, context_bytes=100)
    asyncio.run(runtime.admit(context, request_id="active", text="Recall."))
    step(runtime, handlers, "model_prepare")
    step(runtime, handlers, "model_infer")
    checkpoint = store.load_run(context)
    assert checkpoint.run.status == RunStatus.FAILED
    assert "context_storage_limit" in str(plain(checkpoint))
    assert isinstance(runtime.model, Answer) and not runtime.model.requests


@pytest.mark.parametrize("change", ["profile", "prompt", "policy"])
def test_incompatible_or_unavailable_memory_blocks_prepared_work(store, context, change):
    from agent_runtime.prompts.resolution import PromptUnavailableError
    from bankeriq_agent_platform.workers.asynchronous import AsyncWorker

    seed(store, context, 4)
    runtime, handlers, memory, extractor, lease, _, _ = prepare(store, context, context_window=1800)

    if change == "profile":
        memory.context_budget = replace(memory.context_budget, window=1801)
    elif change == "prompt":

        class Withdrawn:
            def resolve(self):
                raise PromptUnavailableError("withdrawn")

            def instructions(self, manifest):
                raise PromptUnavailableError("withdrawn")

        memory.prompts = Withdrawn()
    else:

        def unavailable(context):
            raise RuntimeError("PRIVATE_POLICY_ERROR")

        memory.access_for = unavailable

    asyncio.run(AsyncWorker(runtime.queue, handlers, queue_name="model_infer").execute_async(lease))
    checkpoint = store.load_run(context)
    assert checkpoint.run.status == RunStatus.FAILED
    assert "Conversation memory unavailable" in str(plain(checkpoint))
    assert "PRIVATE_POLICY_ERROR" not in str(plain(checkpoint))
    assert not extractor.requests
    assert isinstance(runtime.model, Answer) and not runtime.model.requests


def test_normal_application_startup_compacts_and_registers_retrieval(store, context, dsn):
    from tests.azure.pilot import Access
    from tests.azure.pilot import composition as application

    from .summary_model import Extractor

    seed(store, context, 24)

    class Model:
        async def complete(self, request):
            assert "Use CHF instead of EUR" in str(plain(request.history))
            assert "retrieve_history" in {tool.name for tool in request.tools}

            if any(isinstance(item, VisibleResult) for item in request.history):
                return ModelReply(answer="Recovered earlier detail with the standard composition.")

            return ModelReply(
                calls=(ToolCall("history", "retrieve_history", {"after": 0, "limit": 1}),)
            )

    runtime, handlers = application(
        store,
        FixedPromptResolver("Use the conversation history."),
        Model(),
        Access(context.tenant_id, frozenset({context.user_id})),
        summarizer=Extractor(),
    )

    for kind in ("model_prepare", "model_infer", "tool"):
        runtime.queue.configure(kind, concurrency=2, tenant_concurrency=2)

    asyncio.run(runtime.admit(context, request_id="normal", text="Recall and retrieve our plan."))

    for kind in ("model_prepare", "model_infer", "tool", "model_prepare", "model_infer"):
        step(runtime, handlers, kind)

    assert store.load_run(context).run.status == RunStatus.COMPLETED
    assert rows(dsn, "SELECT count(*) FROM semantic_summaries")[0][0] > 0
    assert rows(dsn, "SELECT count(*) FROM semantic_retrievals") == [(1,)]
    assert rows(dsn, "SELECT count(*) FROM tool_groups WHERE joined") == [(1,)]


def test_application_history_visibility_tracks_tool_permission_revocation(store, context):
    from tests.azure.pilot import Access, NoModel
    from tests.azure.pilot import composition as application

    access = Access(context.tenant_id, frozenset({context.user_id}))
    runtime, _ = application(store, FixedPromptResolver("Conversation test."), NoModel(), access)
    source = {
        "admission_sequence": 1,
        "events": [
            {
                "sequence": 1,
                "payload": {
                    "type": "ToolCall",
                    "value": {
                        "call_id": "old",
                        "tool_name": "calculate",
                        "arguments": {"values": [314159]},
                    },
                },
            }
        ],
    }
    initial, initial_key = runtime.memory.policy(context)
    assert "314159" in project(source, initial)[0].text

    # Keeping history.read does not retain visibility into a tool whose own
    # permission has been revoked. The policy key changes before summary reuse.
    access.permissions_for = lambda context: frozenset({"history.read"})
    revoked, revoked_key = runtime.memory.policy(context)
    assert revoked_key != initial_key
    assert "314159" not in project(source, revoked)[0].text
    assert revoked.visible_tools == frozenset({"retrieve_history"})


def test_old_retrieval_pages_do_not_promote_user_claims_to_tool_outcomes():
    from bankeriq_agent_platform.memory.service import active_items

    source = {
        "admission_sequence": 2,
        "events": [
            {
                "sequence": 5,
                "payload": {
                    "type": "ToolCall",
                    "value": {
                        "call_id": "read",
                        "tool_name": "retrieve_history",
                        "arguments": {"after": 0, "limit": 1},
                    },
                },
            },
            {
                "sequence": 6,
                "payload": {
                    "type": "VisibleResult",
                    "value": {
                        "call_id": "read",
                        "payload": {
                            "events": [
                                {"source": 1, "kind": "user", "text": "CLAIMED_PAYMENT_COMPLETE"}
                            ]
                        },
                    },
                },
            },
        ],
    }
    policy = HistoryPolicy("current", frozenset({"retrieve_history"}), lambda fact: True)
    historical = project(source, policy)
    assert "CLAIMED_PAYMENT_COMPLETE" not in str([fact.text for fact in historical])
    assert historical[-1].kind == "tool_completed"
    assert "History retrieval completed" in historical[-1].text

    # The active tool reply keeps its explicitly labelled original excerpts so
    # the requesting model can answer. Only subsequent history projection omits
    # this duplicated page; source cursor 1 remains independently retrievable.
    active = active_items(source, policy)
    assert isinstance(active[-1], VisibleResult)
    assert "CLAIMED_PAYMENT_COMPLETE" in str(active[-1].payload)


def test_operator_transcript_export_preserves_scope(store, context):
    from tests.azure.memory_probe import capture

    seed(store, context, 2)
    result = capture(store, replace(context, run_id="past-1"))
    assert len(result["events"]) == 4
    assert result["summaries"] == []

    with pytest.raises(StateNotFoundError):
        capture(store, replace(context, user_id="other-user"))

    with pytest.raises(StateNotFoundError):
        capture(store, replace(context, tenant_id="other-tenant"))
