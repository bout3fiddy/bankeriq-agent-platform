"""Evaluate reviewed memory cases through the normal durable queued composition."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import TERMINAL_STATUSES, RunStatus
from agent_harness.storage.codec import fingerprint, plain
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import VisibleResult
from agent_runtime.memory.storage import SemanticStore
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from agent_runtime.workers.asynchronous import AsyncWorker
from tests.azure.pilot import Access, composition


class MeasuredModel:
    """Reserve each request before dispatch and retain failed/unknown observations.

    The caller supplies the live adapter and numeric observations from its callback.
    For example, a 5,000-token request with 1,024 reserved output tokens must fit
    before dispatch. Actual known usage releases the unused reservation; missing
    usage retains the full reservation and stops subsequent live calls.
    """

    def __init__(
        self,
        model,
        observations,
        *,
        input_limit,
        output_limit,
        call_limit,
        dispatch_interval=0,
        transport_observations=None,
    ):
        self.model, self.observations = model, observations
        self.transport_observations = (
            transport_observations if transport_observations is not None else []
        )
        self.input_limit, self.output_limit, self.call_limit = (
            input_limit,
            output_limit,
            call_limit,
        )
        self.counter = FoundryTokenCounter("gpt-4o-2024-11-20")
        self.input_tokens = self.output_tokens = 0
        self.calls = []
        self.stopped = False
        self.dispatch_interval = dispatch_interval
        self.next_dispatch = 0

    async def complete(self, request):
        reserved_input = self.counter(request)
        reserved_output = request.max_output_tokens or 2048

        if (
            self.stopped
            or len(self.calls) >= self.call_limit
            or self.input_tokens + reserved_input > self.input_limit
            or self.output_tokens + reserved_output > self.output_limit
        ):
            self.stopped = True
            raise RuntimeError("quality campaign budget exhausted")

        # Explicit test-load pacing keeps a long serial quality suite inside its
        # declared provider envelope. It is outside the generation timer and is
        # never used by the production runtime or a user's conversation turn.
        await asyncio.sleep(max(0, self.next_dispatch - monotonic()))
        self.next_dispatch = monotonic() + self.dispatch_interval
        before = len(self.observations)
        transport_before = len(self.transport_observations)
        started = monotonic()
        retrieval_ids = {
            item.call_id
            for item in request.history
            if isinstance(item, ToolCall) and item.tool_name == "retrieve_history"
        }
        row = {
            "started_utc": datetime.now(UTC).isoformat(),
            "summary": request.output_schema is not None,
            "status": "failed",

            # This evaluator accepts only its reviewed synthetic fixtures. Retain
            # the actual selected input locally so fidelity review can distinguish
            # omitted source text from a model ignoring available evidence.
            "input_history": plain(request.history),
            "retrieved_history": plain(
                [
                    item.payload
                    for item in request.history
                    if isinstance(item, VisibleResult) and item.call_id in retrieval_ids
                ]
            ),
            "instructions_digest": fingerprint(request.instructions),
            "reserved_input_tokens": reserved_input,
            "reserved_output_tokens": reserved_output,
        }

        try:
            reply = await self.model.complete(request)
            row.update(status="completed", answer=reply.answer, tools=plain(reply.calls))
            return reply
        finally:
            observation = self.observations[-1] if len(self.observations) > before else None
            row.update(seconds=monotonic() - started, observation=observation)
            row["transport_timing"] = (
                self.transport_observations[-1]
                if len(self.transport_observations) == transport_before + 1
                else None
            )
            self.calls.append(row)

            if (
                observation is None
                or observation["input_tokens"] is None
                or observation["output_tokens"] is None
            ):
                self.input_tokens += reserved_input
                self.output_tokens += reserved_output
                self.stopped = True
            else:
                self.input_tokens += observation["input_tokens"]
                self.output_tokens += observation["output_tokens"]

                if (
                    self.input_tokens > self.input_limit
                    or self.output_tokens > self.output_limit
                    or observation["input_tokens"] > reserved_input
                    or observation["output_tokens"] > reserved_output
                ):
                    self.stopped = True


class MeasuredStore(SemanticStore):
    """Measure publication independently of provider generation and answer delivery."""

    def __init__(self, store, publications):
        super().__init__(store)
        self.publications = publications

    def publish(self, window, operation, facts, *, fence, retirements=()):
        started = monotonic()
        version = super().publish(window, operation, facts, fence=fence, retirements=retirements)
        self.publications.append(
            {
                "version": version,
                "seconds": monotonic() - started,
                "source_cursor": window.cursor,
                "covered_turn_before": window.covered_turn,
                "previous_version": window.version,
                "policy_key": window.policy_key,
            }
        )
        return version


def seed_turn(store, context, text):
    """Seed an explicitly synthetic completed exchange without claiming live inference.

    These fixtures represent retained history before the evaluated live turn.
    Both user text and the fixed acknowledgement enter the ordinary journal through
    revision-checked storage transitions. Tool outcomes use live queued execution
    separately, so fixture acknowledgements cannot manufacture a completed tool.
    """

    context = replace(context, run_id=str(uuid4()))
    checkpoint = store.admit(
        context, request_id=context.run_id, items=(Message(MessageRole.USER, text),)
    )

    for status in (RunStatus.RUNNING, RunStatus.COMPLETED):
        checkpoint = store.save_checkpoint(
            context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            expected_recovery_id=checkpoint.conversation.recovery_id,
            expected_deletion_generation=checkpoint.conversation.deletion_generation,
            status=status,
            items=(Message(MessageRole.ASSISTANT, "Acknowledged; no action performed."),)
            if status == RunStatus.COMPLETED
            else (),
        )


async def execute(runtime, handlers, context, text, model):
    context = replace(context, run_id=str(uuid4()))
    await runtime.admit(context, request_id=context.run_id, text=text)
    deadline = monotonic() + 180

    # Drive ordinary claims/leases/settlement; the live provider and tools still
    # run through their production adapters. This serial evaluator measures
    # quality and summary duration. It makes no hosted concurrency/latency claim.
    while monotonic() < deadline:
        checkpoint = runtime.queue.store.load_run(context)

        if checkpoint.run.status in TERMINAL_STATUSES:
            return context, checkpoint

        if model.stopped:
            raise RuntimeError("quality campaign stopped before another provider call")

        for kind in ("model_prepare", "model_infer", "tool"):
            lease = runtime.queue.claim(kind, handlers, seconds=30)

            if lease is not None:
                await AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(lease)

        await asyncio.sleep(0.01)

    raise TimeoutError("quality turn did not reach a durable outcome")


async def evaluate(store, prompts, model, cases, output: Path, *, fixture_digest=None):
    filler = " ".join(json.loads(Path("tests/soak/dialogue.json").read_text())[0])
    record = {
        "profile": "live model with seeded synthetic local history",
        "dispatch_interval_seconds": model.dispatch_interval,
        "fixture_sha256": fixture_digest,
        "limits": {
            "model_calls": model.call_limit,
            "input_tokens": model.input_limit,
            "output_tokens": model.output_limit,
        },
        "cases": [],
    }
    output.write_text(json.dumps(record, indent=2) + "\n")
    output.chmod(0o600)

    for repetition in range(2):
        for case in cases:
            context = RunContext("quality-tenant", "quality-operator", str(uuid4()), str(uuid4()))
            store.create_conversation(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                conversation_id=context.conversation_id,
            )
            runtime, handlers = composition(
                store, prompts, model, Access(context.tenant_id, frozenset({context.user_id}))
            )
            publications = []
            assert runtime.memory is not None
            runtime.memory.storage = MeasuredStore(store, publications)
            start_calls = len(model.calls)
            row = {
                "case": case["id"],
                "repetition": repetition,
                "conversation": context.conversation_id,
                "memory_profile": runtime.memory.profile,
            }
            record["cases"].append(row)

            try:
                for text in case["messages"]:
                    text = " ".join(text) if isinstance(text, list) else text

                    if case.get("required_completed_tool"):
                        _, setup = await execute(runtime, handlers, context, text, model)
                        completed = {
                            item.call_id
                            for item in setup.conversation.items
                            if isinstance(item, VisibleResult)
                        }
                        row["completed_tools"] = [
                            item.tool_name
                            for item in setup.conversation.items
                            if isinstance(item, ToolCall) and item.call_id in completed
                        ]

                        if case["required_completed_tool"] not in row["completed_tools"]:
                            raise RuntimeError("required tool did not complete")
                    else:
                        seed_turn(store, context, text)

                # Four ordinary example turns cross the same 8,192-token profile
                # used by the hosted pilot. Long cases continue through five
                # actual durable publications; no summary is injected by this test.
                for _ in range(4):
                    seed_turn(store, context, filler)

                for _ in range(8):
                    await execute(runtime, handlers, context, "Reply with exactly READY.", model)
                    summaries = read_summaries(store, context)

                    if len(summaries) >= case.get("minimum_compactions", 1):
                        break

                    for _ in range(2):
                        seed_turn(store, context, filler)
                else:
                    raise RuntimeError("required compaction cycles were not reached")

                # Keep the assembled pre-question summary for omission review.
                # A retrieval pass requires the detail to have actually disappeared
                # from visible memory, not just a history-tool invocation.
                row["summaries_before_question"] = summaries
                last_answer_input = next(
                    call["input_history"]
                    for call in reversed(model.calls[start_calls:])
                    if not call["summary"]
                )
                question, expected = select_question(case, last_answer_input)
                row["selected_question"] = question
                row["selected_expected"] = expected
                question_start = len(model.calls)
                row["question_call_offset"] = question_start - start_calls
                question_context, checkpoint = await execute(
                    runtime, handlers, context, question, model
                )
                answers = [
                    item.text
                    for item in checkpoint.conversation.items
                    if isinstance(item, Message) and item.role == MessageRole.ASSISTANT
                ]
                answer = answers[-1] if answers else ""
                row.update(status=checkpoint.run.status.value, answer=answer)
                decoded = decode_answer(answer)
                row["facts"] = {
                    key: {
                        "expected": expected,
                        "actual": decoded.get(key),
                        "pass": key in decoded
                        and decoded[key] == expected
                        and (not isinstance(expected, bool) or isinstance(decoded[key], bool)),
                    }
                    for key, expected in expected.items()
                }
                row["critical_pass"] = (
                    all(row["facts"][key]["pass"] for key in case["critical"])
                    and checkpoint.run.status == RunStatus.COMPLETED
                )

                with store._connect() as connection:
                    row["retrieval_calls"] = connection.execute(
                        "SELECT count(*) AS count FROM semantic_retrievals "
                        "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s",
                        (context.tenant_id, context.conversation_id, question_context.run_id),
                    ).fetchone()["count"]

                if case["retrieval"] == "required_after_omission":

                    # The question can itself trigger a summary. Judge omission
                    # against the first answer-model request, whose assembled
                    # history decides whether retrieval was necessary. Summary
                    # source input intentionally contains older evidence.
                    answer_request = next(
                        call for call in model.calls[question_start:] if not call["summary"]
                    )
                    remembered = json.dumps(answer_request["input_history"]).lower()
                    row["source_was_omitted"] = all(
                        str(value).lower() not in remembered for value in expected.values()
                    )

                    # A correct guess after an empty/wrong page is insufficient.
                    # Require the missing detail in an authorised retrieval result
                    # actually supplied to a later answer-model request.
                    retrieved = json.dumps(
                        [call["retrieved_history"] for call in model.calls[question_start:]]
                    ).lower()
                    row["retrieved_expected_source"] = all(
                        str(value).lower() in retrieved for value in expected.values()
                    )
                    row["retrieval_gate"] = (
                        row["source_was_omitted"]
                        and row["retrieval_calls"] > 0
                        and row["retrieved_expected_source"]
                    )
            except Exception as error:
                row.update(status="evaluation_failed", error=type(error).__name__)
            finally:
                row["model_calls"] = model.calls[start_calls:]
                row["publications"] = publications
                record["usage"] = {
                    "model_calls": len(model.calls),
                    "input_tokens": model.input_tokens,
                    "output_tokens": model.output_tokens,
                    "stopped": model.stopped,
                }
                output.write_text(json.dumps(record, indent=2) + "\n")
                print(json.dumps({k: row[k] for k in ("case", "repetition", "status")}), flush=True)

            if model.stopped:
                return record

    return record


def quality_passes(record, cases):
    """Require both repetitions, all critical facts and 95% of other expected facts.

    For example, two complete cases with twenty noncritical checks may miss one.
    A missing repetition, failed retrieval gate or wrong critical amount always
    fails. This numeric gate supplements the separately recorded semantic review;
    exact answers alone cannot establish that every summary is relevant and faithful.
    """

    expected = {(case["id"], repetition) for case in cases for repetition in range(2)}
    observed = {(row["case"], row["repetition"]) for row in record["cases"]}

    if observed != expected or len(record["cases"]) != len(expected):
        return False

    definitions = {case["id"]: case for case in cases}
    noncritical = []

    for row in record["cases"]:
        if not row.get("critical_pass") or not row.get("retrieval_gate", True):
            return False

        case = definitions[row["case"]]
        noncritical.extend(
            row.get("facts", {}).get(key, {}).get("pass", False)
            for key in case["expected"]
            if key not in case["critical"]
        )

    return not noncritical or sum(noncritical) / len(noncritical) >= 0.95


def select_question(case, answer_history):
    """Choose a predeclared detail absent from the previous assembled answer request.

    A summary can correctly retain a lone incidental ribbon color indefinitely.
    A retrieval test therefore supplies a catalog larger than the 24-entry selection
    ceiling, then asks about the first missing detail from a fixed ordered list.
    For example, if code A remains visible but code B is absent, select B's question.
    The ordinary post-question check still requires actual omission and the source
    in a permitted tool result; this choice cannot turn a guessed answer into a pass.
    """

    options = case.get("retrieval_questions")

    if options is None:
        return case["question"], case["expected"]

    visible = json.dumps(answer_history).lower()

    for option in options:
        if all(str(value).lower() not in visible for value in option["expected"].values()):
            return option["question"], option["expected"]

    raise ValueError("retrieval fixture did not produce a missing detail")


def read_summaries(store, context):
    with store._connect() as connection:
        return connection.execute(
            "SELECT version,covered_turn,covered_cursor,prompt_manifest,policy_key,facts "
            "FROM semantic_summaries "
            "WHERE tenant_id=%s AND conversation_id=%s ORDER BY version",
            (context.tenant_id, context.conversation_id),
        ).fetchall()


def decode_answer(answer):
    text = answer.strip()

    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()

    value = json.loads(text)

    if not isinstance(value, dict):
        raise ValueError("expected a JSON object answer")

    return value
