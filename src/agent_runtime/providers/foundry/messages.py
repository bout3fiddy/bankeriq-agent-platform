"""Translate permitted runtime history into Chat Completions messages."""

import json
from itertools import groupby
from typing import cast

from openai.types.chat import ChatCompletionMessageParam

from agent_harness.conversations.conversation import ConversationItem
from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import RestrictedReceipt, ToolFailure, VisibleResult
from agent_harness.types.json_value import JsonValue


def encode_messages(history: tuple[ConversationItem, ...]) -> list[ChatCompletionMessageParam]:
    """Replay text, call batches and completed results without internal wait references."""

    # Approval -> job -> completion can leave three records for one call.
    # Only the final enforced result is a tool response to the external model.
    results = {
        item.call_id: item
        for item in history
        if isinstance(item, (VisibleResult, RestrictedReceipt, ToolFailure))
    }
    messages: list[ChatCompletionMessageParam] = []

    # The coordinator saves each batch's calls together before their results:
    # [call A, call B, result B, result A] becomes one assistant message naming
    # A and B, followed by their two tool messages, matched by call ID.
    for is_call_batch, items in groupby(history, key=lambda item: isinstance(item, ToolCall)):
        if is_call_batch:
            calls = cast(list[ToolCall], list(items))
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.tool_name,
                                "arguments": encode_json(call.arguments),
                            },
                        }
                        for call in calls
                    ],
                }
            )
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": _result_content(results.get(call.call_id)),
                }
                for call in calls
            )
        else:
            for item in items:
                if isinstance(item, Message):
                    messages.append(_text_message(item))

    return messages


def _text_message(message: Message) -> ChatCompletionMessageParam:
    if message.role is MessageRole.USER:
        return {"role": "user", "content": message.text}

    return {"role": "assistant", "content": message.text}


def encode_json(value: JsonValue) -> str:
    """Encode frozen runtime mappings and tuples as ordinary JSON objects and arrays."""

    # ToolCall("a", "sum", {"values": [2, 3]}) freezes the list to a tuple and
    # the dictionary to a mapping proxy. default=dict unwraps proxies for JSON.
    return json.dumps(value, default=dict, allow_nan=False)


def _result_content(result: VisibleResult | RestrictedReceipt | ToolFailure | None) -> str:
    if isinstance(result, VisibleResult):
        return encode_json({"status": "completed", "result": result.payload})

    if isinstance(result, RestrictedReceipt):
        return encode_json({"status": "completed", "output": "restricted"})

    if isinstance(result, ToolFailure):
        return encode_json({"status": "failed", "code": result.code.value})

    # Cancellation can leave a saved call with no result in an earlier turn.
    # Chat Completions requires a reply for every call. This fixed marker tells
    # the model that the outcome is unknown when the user starts another run.
    # Later turns can therefore retain uncertainty about that side effect.
    return encode_json({"status": "unresolved"})
