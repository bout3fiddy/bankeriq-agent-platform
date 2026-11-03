"""Project stored conversation data through current harness visibility policy."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from agent_harness.conversations.conversation import ConversationItem
from agent_harness.conversations.messages import Message
from agent_harness.memory.semantic import MemoryBlocked, MemoryFact
from agent_harness.storage.codec import decode_item
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import PendingToolCall, RestrictedReceipt, VisibleResult


@dataclass(frozen=True)
class HistoryPolicy:
    """Trusted current visibility, with a revision that changes whenever access changes.

    For example, access_for(ctx) returns HistoryPolicy("grants-7", {"calculate"},
    lambda fact: fact.source != revoked_cursor). A new grant revision invalidates
    old summaries. Policy and registry output visibility must both permit a tool's
    content; restricted tool arguments/results and wait/delivery IDs are omitted.
    """

    revision: str
    visible_tools: frozenset[str]
    allows: Callable[[MemoryFact], bool] = field(repr=False, compare=False)

    def permits(self, fact: MemoryFact) -> bool:
        try:
            return self.allows(fact)
        except Exception:
            raise MemoryBlocked("history_access_denied") from None


def project(turn: dict[str, Any], policy: HistoryPolicy) -> tuple[MemoryFact, ...]:
    """Project only known, permitted journal fields; never copy provider reasoning."""

    calls = {
        row["payload"]["value"]["call_id"]: row["payload"]["value"]["tool_name"]
        for row in turn["events"]
        if row["payload"]["type"] == "ToolCall"
    }
    facts: list[MemoryFact] = []

    for row in turn["events"]:
        payload = row["payload"]
        value = payload["value"]
        kind, text = "", ""

        if payload["type"] == "Message":
            kind, text = value["role"], value["text"]
        elif payload["type"] == "ToolCall":
            kind, text = "tool_proposed", "Tool operation proposed; execution is unconfirmed."

            if value["tool_name"] in policy.visible_tools:
                text = json.dumps({"tool": value["tool_name"], "arguments": value["arguments"]})
        elif payload["type"] == "VisibleResult":
            kind, text = (
                "tool_completed",
                "Tool completed; details unavailable under current policy.",
            )

            tool = calls.get(value["call_id"])

            if tool == "retrieve_history" and tool in policy.visible_tools:

                # A retrieved user claim keeps its original source elsewhere in
                # the journal. Re-summarizing a nested page as tool_completed could
                # promote that claim to an action outcome and duplicate old history.
                # Active protocol replies still use the full enforced result below.
                text = "History retrieval completed; original sources remain authoritative."
            elif tool in policy.visible_tools:
                text = json.dumps({"status": "completed", "result": value["payload"]})
        elif payload["type"] == "RestrictedReceipt":
            kind, text = "tool_completed", "Restricted operation completed; output remains private."
        elif payload["type"] == "ToolFailure":
            kind, text = "tool_failed", "Tool failed."
        elif payload["type"] == "PendingToolCall":
            kind, text = "tool_pending", "Tool is pending; completion is unconfirmed."
        else:

            # Unknown records (including stored provider reasoning) are not a
            # new source channel. Coverage can advance past them without export.
            continue

        fact = MemoryFact(row["sequence"], turn["admission_sequence"], kind, text)

        if not policy.permits(fact):
            fact = replace(fact, kind="redacted", text="Source unavailable under current policy.")

        facts.append(fact)

        if payload["type"] == "ToolCall" and fact.kind != "redacted":
            for state, description in (
                ("approved", "An approval was recorded for this exact operation."),
                ("attempted", "Dispatch was prepared; this alone does not confirm completion."),
            ):
                if value["call_id"] in turn.get(state, set()):
                    facts.append(replace(fact, kind="tool_" + state, text=description))

    return tuple(facts)


def active_items(
    turn: dict[str, Any], policy: HistoryPolicy, *, strict: bool = True
) -> tuple[ConversationItem, ...]:
    """Preserve active call/result protocol pairs with current visibility checks."""

    permitted = {fact.source: fact for fact in project(turn, policy)}
    calls = {
        row["payload"]["value"]["call_id"]: row["payload"]["value"]["tool_name"]
        for row in turn["events"]
        if row["payload"]["type"] == "ToolCall"
    }
    items: list[ConversationItem] = []

    for row in turn["events"]:
        fact = permitted.get(row["sequence"])

        if fact is None:
            continue

        if fact.kind == "redacted" and strict:
            raise MemoryBlocked("history_access_denied")

        item = decode_item(row["payload"])

        if isinstance(item, Message) and fact.kind == "redacted":
            item = replace(item, text="Source unavailable under current policy.")
        elif isinstance(item, ToolCall) and (
            fact.kind == "redacted" or item.tool_name not in policy.visible_tools
        ):
            item = replace(item, arguments={})
        elif isinstance(item, VisibleResult) and (
            fact.kind == "redacted" or calls.get(item.call_id) not in policy.visible_tools
        ):
            item = RestrictedReceipt(item.call_id)
        elif isinstance(item, PendingToolCall):
            continue

        items.append(item)

    return tuple(items)
