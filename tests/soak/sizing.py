"""Offline context-pressure estimate; no provider, storage or elapsed-soak evidence."""

import json
from pathlib import Path
from typing import Any

from agent_harness.conversations.conversation import Conversation
from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.model import ModelRequest, ModelTool
from agent_harness.memory.assembly import (
    SUMMARY_INSTRUCTIONS,
    assemble_request,
    plan_compaction,
    summary_request,
)
from agent_harness.memory.projection import HistoryPolicy
from agent_harness.memory.retirement import select_memory
from agent_harness.memory.retrieval import HISTORY_TOOL
from agent_harness.memory.semantic import MemoryFact, TokenBudget, summary_candidates
from agent_harness.memory.window import MemoryWindow
from agent_harness.prompts.resolution import FixedPromptResolver
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunState, RunStatus
from agent_harness.storage.codec import encode_items
from agent_harness.storage.interface import Checkpoint
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from tests.validation.tools import CALCULATE, REPORT


def estimate(turns: int = 39, extra_summary_facts: int = 0) -> dict[str, Any]:
    """Estimate one canary with real tokenizer, prompts, tools and compaction policy.

    The synthetic selector retains the fixed decision and optionally several ordinary
    excerpts. For example, estimate(39, 4) measures pressure when four disposable
    excerpts survive each update. It does not predict actual model choices, retries,
    retrieval calls, billed tokens or duration. Live publication counts remain the gate.
    """

    counter = FoundryTokenCounter("gpt-4o-2024-11-20")
    text = " ".join(json.loads(Path("tests/soak/dialogue.json").read_text())[0])
    prompts = load_catalog().resolver()
    manifest = prompts.resolve()
    tools = tuple(
        ModelTool(tool.name, tool.description, tool.argument_schema)
        for tool in (CALCULATE, REPORT, HISTORY_TOOL)
    )
    base = ModelRequest((), tools, instructions=prompts.instructions(manifest))
    summary_prompts = FixedPromptResolver(
        "Memory format version 2. Select at most 24 concise candidate identifiers. "
        "Prioritize user decisions, corrections, names, responsibilities, open questions "
        "and tool outcomes. Keep user identifiers, including names and nicknames. Omit routine "
        "acknowledgements and illustrative examples. Return raw JSON only."
    )
    summary_manifest = summary_prompts.resolve()
    instructions = SUMMARY_INSTRUCTIONS + "\n" + summary_prompts.instructions(summary_manifest)
    context_budget = TokenBudget("soak-context", 8192, 512, counter)
    summary_budget = TokenBudget("soak-summary", 8192, 1024, counter)
    policy = HistoryPolicy("soak", frozenset({"calculate", "retrieve_history"}), lambda _: True)
    context = RunContext("synthetic", "operator", "canary", "run")
    checkpoint = Checkpoint(
        Conversation("synthetic", "operator", "canary"), RunState(context, RunStatus.RUNNING)
    )
    facts: tuple[MemoryFact, ...] = ()
    completed: list[dict[str, Any]] = []
    summary_tokens: list[int] = []
    coordinator_tokens: list[int] = []
    output_tokens = 0
    version = 0
    covered_turn = 0

    # Two recall turns preserve the real workload's short-message breaks. Ordinary
    # turns carry the 1,050-token example; replies are READY. Only the first decision
    # survives the ideal selector, keeping fact growth independent of dialogue length.
    for turn in range(1, turns + 1):
        recall = turn in (max(3, turns // 3), turns)
        message = text

        if turn == 1:
            message = (
                "Decision: the permanent verification code for this fictional session is "
                "SOAK-CANARY-1. Preserve it across the following planning dialogue. "
                "It is separate from the fictional project's name. Reply with exactly READY."
            )
        elif recall:
            message = (
                "What permanent verification code did I give for this fictional session? "
                "Retrieve earlier history if needed. Reply with that code only."
            )

        active = journal_turn(turn, message)
        compacting = False

        for _ in range(9):
            window = MemoryWindow(
                checkpoint,
                turn * 2,
                turn,
                version,
                covered_turn,
                covered_turn * 2,
                facts,
                tuple(completed),
                active,
                False,
                "soak",
            )
            request = assemble_request(window, base, policy, context_budget.output)
            free = context_budget.window - counter(request) - context_budget.output
            compacting = compacting or free < context_budget.window // 5

            if context_budget.fits(request) and (
                not compacting or free >= context_budget.window * 2 // 5
            ):
                coordinator_tokens.append(counter(request))
                break

            plan = plan_compaction(
                window,
                base,
                policy,
                instructions,
                summary_manifest,
                None,
                context_budget=context_budget,
                summary_budget=summary_budget,
                keep_recent_turns=2,
                target_free_tokens=context_budget.window * 2 // 5,
            )

            if plan.partial:
                raise AssertionError("the fixture must permit whole-turn compaction")

            summary = summary_request(plan.sources, instructions, summary_manifest, 1024)
            candidates = summary_candidates(plan.sources)
            selected = [index for index, fact in enumerate(candidates) if fact.label != "fact"]
            ordinary = [index for index, fact in enumerate(candidates) if fact.label == "fact"]
            selected += ordinary[:extra_summary_facts]
            answer = json.dumps(
                {
                    "entries": [
                        {"candidate": index, "label": candidates[index].label} for index in selected
                    ],
                    "retirements": [],
                }
            )
            facts = select_memory(answer, candidates, plan.sources).facts
            summary_tokens.append(counter(summary))
            output_tokens += len(counter.encoding.encode(answer, disallowed_special=()))
            version += 1
            covered_turn = plan.turns[-1]["admission_sequence"]
            completed = [item for item in completed if item["admission_sequence"] > covered_turn]
        else:
            raise AssertionError("fixture exceeded the bounded compaction loop")

        answer = "SOAK-CANARY-1" if recall else "READY"
        output_tokens += len(counter.encoding.encode(answer, disallowed_special=()))
        completed.append(journal_turn(turn, message, answer))

    return {
        "evidence": "offline_projection_with_synthetic_summary_selection",
        "turns_per_canary": turns,
        "extra_summary_facts": extra_summary_facts,
        "message_utf8_bytes": len(text.encode()),
        "message_text_tokens": len(counter.encoding.encode(text, disallowed_special=())),
        "summary_versions": version,
        "coordinator_input_tokens": sum(coordinator_tokens),
        "summary_input_tokens": sum(summary_tokens),
        "total_input_tokens": sum(coordinator_tokens) + sum(summary_tokens),
        "output_tokens": output_tokens,
        "model_calls": turns + version,
        "max_coordinator_input_tokens": max(coordinator_tokens),
        "max_summary_input_tokens": max(summary_tokens, default=0),
    }


def journal_turn(turn: int, text: str, answer: str | None = None) -> dict[str, Any]:
    items = (Message(MessageRole.USER, text),)

    if answer is not None:
        items += (Message(MessageRole.ASSISTANT, answer),)

    return {
        "admission_sequence": turn,
        "status": RunStatus.RUNNING if answer is None else RunStatus.COMPLETED,
        "events": [
            {"sequence": turn * 2 + index, "payload": payload}
            for index, payload in enumerate(encode_items(items))
        ],
    }


if __name__ == "__main__":
    print(json.dumps([estimate(turns, extra) for turns in (32, 39, 41) for extra in (0, 4)]))
