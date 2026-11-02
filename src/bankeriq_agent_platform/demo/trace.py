"""Explain the recorded model/tool feedback loop in plain language."""

import json
from itertools import groupby
from typing import cast

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import RestrictedReceipt, ToolFailure, ToolResult, VisibleResult


def explain_history(history: tuple[ConversationItem, ...]) -> str:
    """Describe observed inputs, requested actions and results in their saved order."""

    sections = [
        "## Execution trace",
        "Each model call receives the instructions, available tools and conversation so far. "
        "The new input below shows what was added since its previous reply.",
    ]
    tool_names = {item.call_id: item.tool_name for item in history if isinstance(item, ToolCall)}
    new_inputs: list[str] = []
    model_call = 0

    # Consecutive calls belong to one model reply. For example, a reply requesting
    # tools A and B followed by their results is one model call with two actions.
    for is_tool_batch, items in groupby(history, key=lambda item: isinstance(item, ToolCall)):
        batch = list(items)

        if is_tool_batch:
            model_call += 1
            sections.extend(_model_input(model_call, new_inputs))
            new_inputs.clear()

            for call in cast(list[ToolCall], batch):
                arguments = json.dumps(call.arguments, default=dict, ensure_ascii=False)
                sections.append(f"**Model requests:** `{call.tool_name}({arguments})`.")

            continue

        for item in cast(list[Message | ToolResult], batch):
            if isinstance(item, Message):
                if item.role is MessageRole.USER:
                    sections.append(f"### User request\n\n> {item.text}")
                    new_inputs.append("The user request above.")
                else:
                    model_call += 1
                    sections.extend(_model_input(model_call, new_inputs))
                    new_inputs.clear()
                    sections.append(f"**Model answers:**\n\n> {item.text}")

            elif isinstance(item, VisibleResult):
                payload = json.dumps(item.payload, default=dict, ensure_ascii=False)
                result = f"`{tool_names[item.call_id]}` returned `{payload}`."
                sections.append(f"**Tool result:** {result}")
                sections.append("The runtime saves this permitted result for the next model call.")
                new_inputs.append(result)

            elif isinstance(item, RestrictedReceipt):
                result = (
                    f"`{tool_names[item.call_id]}` completed. "
                    'The model receives `{"status": "completed", "output": "restricted"}`.'
                )
                sections.append(f"**Completion received:** {result}")
                sections.append(
                    "Report contents stay internal. This receipt lets the loop continue."
                )
                new_inputs.append(result)

            elif isinstance(item, ToolFailure):
                result = f"`{tool_names[item.call_id]}` failed with code `{item.code.value}`."
                sections.append(f"**Tool failure:** {result}")
                new_inputs.append(result)

            else:
                sections.append(
                    f"**Runtime pauses:** `{tool_names[item.call_id]}` is waiting for "
                    f"`{item.reason.value}` completion. Model calls stop while the run waits. "
                    "The continuation handler resumes the run when completion is supplied."
                )

    sections.append("Per-step timing and token usage were not recorded.")
    return "\n\n".join(sections)


def _model_input(number: int, inputs: list[str]) -> list[str]:
    return [f"### Model call {number}", "**New input:** " + " ".join(inputs)]
