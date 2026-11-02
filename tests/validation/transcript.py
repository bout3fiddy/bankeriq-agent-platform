"""Readable transcripts for the fixed synthetic validation workflow."""

import json
from datetime import UTC, datetime
from typing import TextIO

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.conversations.messages import Message
from agent_runtime.storage.interface import Checkpoint
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import RestrictedReceipt, ToolFailure, VisibleResult

from .trace import explain_history


def write_transcript(
    destination: TextIO,
    checkpoint: Checkpoint,
    *,
    instructions: str,
    model_label: str,
    resumptions: int,
) -> None:
    """Export recorded conversation items after the validator's output-isolation check."""

    run = checkpoint.run
    sections = [
        "# Synthetic agent conversation",
        f"Captured: {datetime.now(UTC).isoformat()}",
        f"Model selection: {model_label}",
        f"Run: {run.context.run_id}",
        f"Status: {run.status.value}; model calls: {run.model_steps}; "
        f"tool calls: {run.tool_calls}; job resumptions: {resumptions}.",
        "The coordinator ran locally with synthetic tools and in-memory state. "
        "Entries below come from its recorded conversation. Runtime wait events are "
        "labelled separately. Standard BankerIQ instructions were used; "
        "scoring stayed outside model context.",
        "## Instructions supplied to the model",
        instructions,
        explain_history(checkpoint.conversation.items),
        "## Recorded fields",
        "<details>\n<summary>Show exact recorded fields and call IDs</summary>\n",
    ]

    for number, item in enumerate(checkpoint.conversation.items, start=1):
        label, content = _entry(item)

        # JSON preserves exact message text and arguments, including line breaks.
        # For example, a calculate call records {"values": [12, 30]} and its
        # matching result records {"total": 42}; call IDs connect the two entries.
        encoded = json.dumps(content, default=dict, indent=2, ensure_ascii=False)
        sections.append(f"### {number}. {label}\n\n```json\n{encoded}\n```")

    sections.append("</details>")
    destination.write("\n\n".join(sections) + "\n")
    destination.flush()


def _entry(item: ConversationItem) -> tuple[str, dict[str, object]]:
    if isinstance(item, Message):
        return item.role.value.capitalize(), {"text": item.text}

    if isinstance(item, ToolCall):
        return "Assistant tool call", {
            "call_id": item.call_id,
            "tool": item.tool_name,
            "arguments": item.arguments,
        }

    if isinstance(item, VisibleResult):
        return "Tool result visible to the model", {
            "call_id": item.call_id,
            "status": "completed",
            "result": item.payload,
        }

    if isinstance(item, RestrictedReceipt):

        # The report body and artifact location stay on the delivery path. The
        # model receives only this completion receipt, so that is what we export.
        return "Restricted tool receipt visible to the model", {
            "call_id": item.call_id,
            "status": "completed",
            "output": "restricted",
        }

    if isinstance(item, ToolFailure):
        return "Tool failure visible to the model", {
            "call_id": item.call_id,
            "status": "failed",
            "code": item.code.value,
        }

    return "Runtime waiting", {"call_id": item.call_id, "reason": item.reason.value}
