"""Twenty synthetic source-selection cases against the private live model."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from time import monotonic

import httpx2
from openai import AsyncOpenAI

from agent_runtime.conversations.retirement import select_memory
from agent_runtime.conversations.semantic import MemoryFact, TokenBudget, summary_candidates
from agent_runtime.coordinator.model import ModelRateLimited
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from bankeriq_agent_platform.memory.service import (
    SUMMARY_INSTRUCTIONS,
    HistoryPolicy,
    SemanticMemory,
)
from bankeriq_agent_platform.memory.storage import SemanticStore
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from tests.azure.exec_transport import LocalRelayTransport


async def main(args):
    if args.output.exists():
        raise ValueError("use a new output path")

    observations = []
    prompts = FixedPromptResolver("Select current useful memory from original sources.")
    counter = FoundryTokenCounter("gpt-4o-2024-11-20")
    cases = []

    for case in json.loads(Path("performance/workloads/summary_cases.json").read_text()):
        facts = tuple(
            MemoryFact(**{**fact, "text": "".join(fact["text"])}) for fact in case["sources"]
        )
        cases.append((case["name"], facts, case["expected"]))

    policy = HistoryPolicy("fixture", frozenset({"calculate", "retrieve_history"}), lambda f: True)
    record = {"workload": "four source fixtures, five repetitions each", "attempts": []}
    output = args.output

    # The socket relays only synthetic model calls. No database operation is
    # performed: SemanticMemory supplies the same request builder and source
    # validation used by workers. Keep failed calls and ambiguous retirement
    # suggestions alongside successful selections for quality review.
    async with (
        httpx2.AsyncClient(transport=LocalRelayTransport(args.socket)) as http,
        AsyncOpenAI(
            api_key="relay-only",
            base_url="https://relay.invalid/v1/",
            http_client=http,
            max_retries=0,
        ) as sdk,
    ):
        model = AsyncFoundryModelClient(
            sdk, deployment="coordinator", observer=lambda r: observations.append(asdict(r))
        )
        memory = SemanticMemory(
            SemanticStore(
                PostgresStateStore("postgresql://postgres:synthetic@127.0.0.1:55449/postgres")
            ),
            model,
            prompts=prompts,
            summary_budget=TokenBudget("summary", 8192, 1024, counter),
            context_budget=TokenBudget("context", 8192, 512, counter),
            access_for=lambda c: policy,
        )

        for repetition in range(5):
            for name, sources, expected in cases:
                request = memory._summary_request(sources, SUMMARY_INSTRUCTIONS, prompts.resolve())
                memory.summary_budget.require(request)

                for attempt in range(1, 4):
                    started = monotonic()
                    row = {
                        "case": name,
                        "repetition": repetition,
                        "attempt": attempt,
                        "estimated_input_tokens": counter(request),
                    }

                    try:
                        reply = await model.complete(request)
                        row["seconds"] = round(monotonic() - started, 6)
                        selection = select_memory(
                            reply.answer, summary_candidates(sources), sources
                        )
                        facts = [asdict(f) for f in selection.facts]
                        joined = " ".join(f["text"] for f in facts)
                        row.update(
                            status="completed",
                            facts=facts,
                            retirements=selection.retirements,
                            missing=[s for s in expected if s not in joined],
                            unsupported_action=any(
                                f["label"] == "completed_action" and f["kind"] != "tool_completed"
                                for f in facts
                            ),
                        )

                        if name in {"explicit-correction", "structured-update"}:
                            row["unresolved_questions"] = [
                                f["text"]
                                for f in facts
                                if f["label"] == "open_question"
                                and (
                                    "languages" in f["text"].lower()
                                    or "reviewer" in f["text"].lower()
                                )
                            ]
                    except ModelRateLimited as error:
                        row.update(
                            status="rate_limited",
                            seconds=round(monotonic() - started, 6),
                            retry_after=error.retry_after,
                        )
                    except Exception as error:
                        row.update(
                            status=type(error).__name__, seconds=round(monotonic() - started, 6)
                        )

                    row["observation"] = observations[-1] if observations else None
                    record["attempts"].append(row)
                    output.write_text(json.dumps(record, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                k: v
                                for k, v in row.items()
                                if k not in {"facts", "retirements", "observation"}
                            }
                        ),
                        flush=True,
                    )

                    if row["status"] != "rate_limited":
                        break

                    await asyncio.sleep(row["retry_after"])


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--socket", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
asyncio.run(main(parser.parse_args()))
