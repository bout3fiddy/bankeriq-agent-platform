"""Live Foundry conversation with local state and fresh, independent queued workers.

Run send repeatedly with fictional messages; render produces the complete transcript,
including every durable summary published during a turn. Cloud resources are never changed.
The driver uses the normal pilot composition, budgets, gateway and pinned prompt catalogue.
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx2
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncOpenAI
from psycopg.conninfo import conninfo_to_dict

from agent_runtime.hosting.access import Principal
from agent_runtime.prompts.resolution import PromptScope
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import TERMINAL_STATUSES
from bankeriq_agent_platform.prompt_files.loader import load_catalog
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.storage.codec import plain
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate
from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver, PromptAdministration
from bankeriq_agent_platform.workers.asynchronous import AsyncWorker
from tests.azure.exec_transport import LocalRelayTransport
from tests.azure.pilot import Access, NoModel, composition

SCOPE = PromptScope("bankeriq", "pilot")


class MeasuredModel:
    def __init__(self, model, kind, path):
        self.model, self.kind, self.path = model, kind, path
        self.count = FoundryTokenCounter("gpt-4o-2024-11-20")

    async def complete(self, request):
        started = time.monotonic()

        try:
            result = await self.model.complete(request)
        except Exception as error:
            with self.path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "kind": self.kind,
                            "error": type(error).__name__,
                            "seconds": round(time.monotonic() - started, 3),
                            "estimated_input_tokens": self.count(request),
                            "reserved_output_tokens": request.max_output_tokens,
                            "tools_called": [],
                        }
                    )
                    + "\n"
                )

            raise

        record = {
            "kind": self.kind,
            "seconds": round(time.monotonic() - started, 3),
            "estimated_input_tokens": self.count(request),
            "reserved_output_tokens": request.max_output_tokens,
            "tools_called": [call.tool_name for call in result.calls],
        }

        # The user explicitly requested a synthetic transcript with the actual
        # model summary. Keep it in that artifact, separate from runtime telemetry.
        if self.kind == "summary":
            record["summary_reply"] = result.answer

        with self.path.open("a") as output:
            output.write(json.dumps(record) + "\n")

        return result


def dependencies(*, initialise_schema=False):
    dsn = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]

    # This harness may migrate a disposable database. Refuse cloud addresses even
    # when an operator accidentally supplies the retained deployment's connection.
    if conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("live-model test state must use a local PostgreSQL host")

    if initialise_schema:
        migrate(dsn)

    store = PostgresStateStore(dsn)
    prompts = PostgresPromptResolver(dsn, SCOPE)
    return dsn, store, prompts


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def initialise(path, dsn, store):
    if path.exists():
        return json.loads(path.read_text())

    actor = Principal("synthetic-memory-tenant", "synthetic-operator")
    administration = PromptAdministration(dsn, SCOPE, authorize=lambda p, s: p == actor)
    administration.publish_definition(actor, request_id="memory-publish", validation_reference="11")
    selected = load_catalog().catalog.defaults.system
    version = f"{selected.id}@{selected.version}"
    active, revision = administration.active_binding(actor)

    if active != version:
        administration.activate(
            actor, request_id="memory-activate", version=version, expected_revision=revision
        )

    context = RunContext(actor.tenant_id, actor.user_id, str(uuid4()), str(uuid4()))
    store.create_conversation(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
    )
    state = {
        "context": plain(context),
        "turns": [],
        "summaries": [],
        "memory_profile": "structured-v6",
    }
    save(path, state)
    return state


async def worker(args):
    _, store, prompts = dependencies()
    state = json.loads(args.state.read_text())
    context = RunContext(**state["context"])
    access = Access(context.tenant_id, frozenset({context.user_id}))

    async with AsyncExitStack() as stack:
        model, summarizer = NoModel(), NoModel()

        if args.role == "coordinator":
            credential = await stack.enter_async_context(AzureCliCredential())
            transport = None

            if socket_path := os.environ.get("BANKERIQ_TEST_RELAY_SOCKET"):
                transport = LocalRelayTransport(socket_path)

            http_client = await stack.enter_async_context(httpx2.AsyncClient(transport=transport))
            client = await stack.enter_async_context(
                AsyncOpenAI(
                    base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],
                    api_key=get_bearer_token_provider(
                        credential, "https://cognitiveservices.azure.com/.default"
                    ),
                    max_retries=0,
                    http_client=http_client,
                    timeout=120,
                )
            )

            def observed(record):
                value = {
                    **asdict(record),
                    "run": context.run_id,
                    "time": datetime.now(UTC).isoformat(),
                }

                with args.state.with_suffix(".usage.jsonl").open("a") as output:
                    output.write(json.dumps(value) + "\n")

            provider = AsyncFoundryModelClient(
                client, deployment="coordinator", max_completion_tokens=2048, observer=observed
            )
            metrics = args.state.with_suffix(".calls.jsonl")
            model = MeasuredModel(provider, "coordinator", metrics)
            summarizer = MeasuredModel(provider, "summary", metrics)

        runtime, handlers = composition(store, prompts, model, access, summarizer=summarizer)
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        kinds = ("model_prepare", "model_infer") if args.role == "coordinator" else ("tool",)
        await asyncio.gather(
            *(
                AsyncWorker(
                    runtime.queue, handlers, queue_name=kind, concurrency=1, poll_seconds=0.1
                ).run_async(stop)
                for kind in kinds
            )
        )


async def send(args):
    dsn, store, prompts = dependencies(initialise_schema=not args.state.exists())
    state = initialise(args.state, dsn, store)
    context = RunContext(**{**state["context"], "run_id": str(uuid4())})
    state["context"] = plain(context)
    save(args.state, state)
    access = Access(context.tenant_id, frozenset({context.user_id}))
    runtime, _ = composition(store, prompts, NoModel(), access)

    for kind in ("model_prepare", "model_infer", "tool"):
        runtime.queue.configure(kind, concurrency=4, tenant_concurrency=2)

    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])
    sessions = SessionService(store, cache)
    calls_path = args.state.with_suffix(".calls.jsonl")
    before = len(calls_path.read_text().splitlines()) if calls_path.exists() else 0
    started = time.monotonic()
    await runtime.admit(context, request_id=context.run_id, text=args.text)
    admission_seconds = time.monotonic() - started
    sessions.record_activity(context)
    processes = []

    try:

        # Fresh coordinator and tool processes establish that context survives
        # interpreter replacement on every turn. Only the coordinator authenticates
        # to Foundry. Workers shut down after the accepted run reaches a terminal state.
        for role in ("coordinator", "tool"):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.azure.semantic_chat",
                        "worker",
                        "--state",
                        str(args.state),
                        "--role",
                        role,
                    ]
                )
            )

        deadline = time.monotonic() + 600

        while time.monotonic() < deadline:
            checkpoint = store.load_run(context)

            if checkpoint.run.status in TERMINAL_STATUSES:
                break

            with store._connect() as connection:
                stopped = connection.execute(
                    "SELECT 1 FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                    "AND status='stopped' LIMIT 1",
                    store._key(context),
                ).fetchone()

            if stopped:
                raise RuntimeError("live test exhausted its bounded worker attempts")

            if any(process.poll() is not None for process in processes):
                raise RuntimeError("live test worker exited before completion")

            await asyncio.sleep(0.2)
        else:
            raise TimeoutError("live conversation turn exceeded 600 seconds")

        # Read this conversation's exact durable records. Payloads are written only
        # to the explicitly requested synthetic transcript, never application logs.
        with store._connect() as connection:
            events = connection.execute(
                "SELECT sequence,payload FROM conversation_events WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s ORDER BY sequence",
                store._key(context),
            ).fetchall()
            summaries = connection.execute(
                "SELECT version,covered_turn,covered_cursor,prompt_manifest,facts,partial_turn,"
                "retained_cursor,retirements "
                "FROM semantic_summaries WHERE tenant_id=%s AND conversation_id=%s "
                "ORDER BY version",
                store._scope(context),
            ).fetchall()

        replies = [
            row["payload"]["value"]["text"]
            for row in events
            if row["payload"]["type"] == "Message"
            and row["payload"]["value"]["role"] == "assistant"
        ]
        view = sessions.read(context)
        turn = {
            "number": len(state["turns"]) + 1,
            "run_id": context.run_id,
            "user": args.text,
            "assistant": replies,
            "status": checkpoint.run.status,
            "cache_cursor": view["cursor"],
            "worker_pids": [p.pid for p in processes],
            "admission_seconds": round(admission_seconds, 3),
            "turn_seconds": round(time.monotonic() - started, 3),
            "new_summaries": summaries[len(state["summaries"]) :],
            "model_calls": [
                json.loads(line) for line in calls_path.read_text().splitlines()[before:]
            ]
            if calls_path.exists()
            else [],
            "events": events,
        }
        state["turns"].append(turn)
        state["summaries"] = summaries
        save(args.state, state)
        print(
            json.dumps(
                {
                    key: turn[key]
                    for key in (
                        "number",
                        "status",
                        "assistant",
                        "turn_seconds",
                        "new_summaries",
                        "model_calls",
                    )
                },
                indent=2,
            )
        )
    finally:
        for process in processes:
            process.terminate()

        for process in processes:
            process.wait(timeout=60)

        cache.close()


def render(args):
    state = json.loads(args.state.read_text())
    lines = [
        "# Live semantic-memory conversation",
        "",
        "Fictional dialogue driven by Codex. Responses and summaries were generated by the live",
        "Azure Foundry gpt-4o-2024-11-20 deployment. Runtime workers, PostgreSQL and Redis ran",
        "locally; the existing hosted application remained unchanged. Each turn started fresh",
        "coordinator and tool processes. Inference used an ephemeral helper in the existing",
        "coordinator through its authenticated exec channel to reach private Foundry. These",
        "timings include that relay, process startup and polling; they are not hosted latency.",
        "After the recorded failed turn, dispatch was paced within an 8,500 estimated-token",
        "allowance per 65 seconds on the shared deployment. Those waits are included in timings.",
        "",
        "Final default context budget: 8,192 tokens including 512 output tokens. Summary budget:",
        "8,192 tokens including 2,048 output tokens. Counters are conservative local estimates.",
        "Published summaries show PostgreSQL content after excerpt validation and preservation",
        "of protected decisions, open questions and recorded action states.",
        "Early attempts used a 16,384-token summary window and model-generated excerpts.",
        "Their rejected results remain visible below. Turns 20 onward use candidate selection;",
        "turn 22 also excludes nested history-result pages from later summary projections.",
        "",
    ]

    if args.hosted_snapshot is not None:
        snapshot = json.loads(args.hosted_snapshot.read_text())

        if snapshot["conversation"] != state["conversation"]:
            raise ValueError("hosted transcript and database export have different scopes")

        # A summary operation records the run that published its version. Place
        # that exact database content before that run's reply, without asking the
        # model to reconstruct or describe its own hidden context.
        owners = {
            row["result_version"]: row["run_id"]
            for row in snapshot["operations"]
            if row["result_version"] is not None
        }

        for turn in state["turns"]:
            turn["assistant"] = turn["replies"]
            turn["model_calls"] = []
            turn["new_summaries"] = [
                row for row in snapshot["summaries"] if owners[row["version"]] == turn["run"]
            ]

        lines = [
            "# Hosted semantic-memory conversation",
            "",
            "Fictional dialogue driven by Codex through the authenticated Azure runtime API.",
            "API, coordinator, tools, PostgreSQL and Redis ran in the retained Azure deployment.",
            "The live model was Foundry gpt-4o-2024-11-20. Summary text below is exported from",
            "the owned PostgreSQL records; it was not reconstructed from an assistant answer.",
            "",
            "Compaction and history retrieval use the normal application defaults. Context:",
            "8,192 estimated tokens including 512 output tokens. Summary: 8,192 including 2,048.",
            "The single coordinator shares an 8,500-token allowance per 65 seconds. Turn times",
            "include HTTP admission, quota waits, model/tool work, polling and final history read;",
            "token acquisition is excluded. Individual model-call timings were not captured.",
            "These correctness samples do not establish a latency percentile or service target.",
            "",
            f"Conversation: `{state['conversation']}`.",
            "",
        ]

    if state.get("memory_profile") == "structured-v6":
        lines = [
            "# Live automatic-compaction conversation",
            "",
            "Fictional dialogue using the normal queued runtime and live Azure Foundry model.",
            (
                "API, coordinator, tools, PostgreSQL and Redis run in the Azure deployment."
                if args.hosted_snapshot is not None
                else "Workers restart on every local turn; PostgreSQL and Redis retain the chat."
            ),
            "Context: 8,192 estimated tokens including 512 reserved output tokens. Summary:",
            "8,192 including 1,024 output tokens. Automatic trigger reserve is 20%, target 40%.",
            "The two-turn verbatim tail is constrained by the full request budget. Active user",
            "input stays verbatim when completed earlier exchanges are compacted within a turn.",
            "There is no artificial quota pacing. Real provider rejections use durable retries.",
            (
                "Timings include admission, model/tool work, polling and final history retrieval."
                if args.hosted_snapshot is not None
                else "Timings include local process startup, private inference relay and polling."
            ),
            "These mixed correctness samples do not establish latency percentiles.",
            "Failed attempts remain in the ledger. Token acquisition is excluded.",
            "",
        ]

    for turn in state["turns"]:
        lines.extend([f"## Turn {turn['number']}", "", "**User (test driver)**", ""])
        lines.extend(textwrap.wrap(turn["user"], width=96))

        for summary in turn["new_summaries"]:
            lines.extend(
                [
                    "",
                    f"**Summary v{summary['version']} published before the reply**",
                    "",
                    f"Covers turns through {summary['covered_turn']}; "
                    f"source cursor {summary['covered_cursor']}.",
                    "Prompt hash: " + summary["prompt_manifest"]["content_hash"],
                    "",
                ]
            )

            if summary.get("partial_turn"):
                lines.extend(
                    [
                        "This is partial-turn coverage; the latest exchange remains verbatim.",
                        f"Retained user input source: {summary.get('retained_cursor', 0)}.",
                        "",
                    ]
                )

            for change in summary.get("retirements", []):
                lines.append(
                    f"Retirement: source {change['previous']['source']} "
                    f"{change['relation']} by source {change['replacement']['source']}."
                )

            for fact in summary["facts"]:
                lines.append(
                    f"- Source {fact['source']}, turn {fact['turn']}, "
                    f"{fact['kind']}, {fact['label']}:"
                )
                lines.extend(
                    textwrap.wrap(
                        fact["text"], width=94, initial_indent="  ", subsequent_indent="  "
                    )
                )

        if failure := turn.get("failure"):
            lines.extend(["", "**Recorded failure and recovery**", ""])
            lines.extend(textwrap.wrap(failure, width=96))

        lines.extend(["", "**Runtime assistant**", ""])

        for reply in turn["assistant"]:
            for paragraph in reply.splitlines():
                lines.extend(textwrap.wrap(paragraph, width=96) or [""])

        duration = turn["turn_seconds"]
        timing = f"{duration} seconds" if duration is not None else "not recorded"
        lines.extend(["", f"Status: {turn['status']}. Turn: {timing}.", ""])

        for call in turn["model_calls"]:
            if error := call.get("error"):
                lines.append(f"- Failed {call['kind']} attempt: {error}; {call['seconds']} s.")
                continue

            lines.append(
                f"- {call['kind']}: {call['seconds']} s; "
                f"estimated input {call['estimated_input_tokens']} tokens; "
                f"tools {', '.join(call['tools_called']) or 'none'}."
            )

        for loss in state.get("cache_losses", []):
            if loss["after_turn"] == turn["number"]:
                lines.extend(
                    [
                        "",
                        "**Redis view removed after this turn**",
                        "",
                        f"Deleted view keys: {loss['deleted_view_keys']}. "
                        f"Durable cursor: {loss['durable_cursor']}; "
                        f"summary versions: {loss['summary_versions']}.",
                    ]
                )

        lines.append("")

    args.output.write_text("\n".join(lines))
    print(str(args.output))


def clear_cache(args):
    _, store, _ = dependencies()
    state = json.loads(args.state.read_text())
    context = RunContext(**state["context"])
    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])

    try:
        sessions = SessionService(store, cache)
        before = sessions.read(context)
        head = sessions.history.head(context)
        deleted = cache.client.delete(cache.key(head))
        result = {
            "after_turn": len(state["turns"]),
            "deleted_view_keys": deleted,
            "durable_cursor": before["cursor"],
            "summary_versions": len(state["summaries"]),
        }
        state.setdefault("cache_losses", []).append(result)
        save(args.state, state)
        print(json.dumps(result))
    finally:
        cache.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["send", "worker", "render", "clear-cache", "replay"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--text")
    parser.add_argument("--role", choices=["coordinator", "tool"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dialogue", type=Path)
    parser.add_argument("--hosted-snapshot", type=Path)
    args = parser.parse_args()

    if args.action == "send":
        if not args.text:
            parser.error("send requires --text with fictional content")

        asyncio.run(send(args))
    elif args.action == "replay":
        if args.dialogue is None:
            parser.error("replay requires --dialogue")

        for parts in json.loads(args.dialogue.read_text()):
            args.text = " ".join(parts)
            asyncio.run(send(args))

            if json.loads(args.state.read_text())["turns"][-1]["status"] != "completed":
                raise RuntimeError("live dialogue turn did not complete")
    elif args.action == "worker":
        asyncio.run(worker(args))
    elif args.action == "clear-cache":
        clear_cache(args)
    else:
        render(args)


if __name__ == "__main__":
    main()
