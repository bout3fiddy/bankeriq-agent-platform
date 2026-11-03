"""Validate provider replies before creating runtime tool calls or answers."""

import json

from openai.types.chat import ChatCompletion, ChatCompletionMessageToolCallUnion

from agent_harness.coordinator.model import ModelReply
from agent_harness.tools.definitions import ToolCall


class FoundryModelError(RuntimeError):
    """Fixed adapter failure text suitable for runtime error handling."""


def decode_reply(response: ChatCompletion) -> ModelReply:
    """Accept one finished text answer or a complete batch of function calls."""

    if len(response.choices) != 1:
        raise FoundryModelError("expected one model choice")

    choice = response.choices[0]
    message = choice.message

    # Example: text="The total is ...", finish_reason="length" is truncated.
    # Reject it before the coordinator could save it as a completed answer.
    if choice.finish_reason not in ("stop", "tool_calls"):
        raise FoundryModelError("model response did not complete")

    if message.function_call is not None or message.audio is not None:
        raise FoundryModelError("unsupported model response")

    if choice.finish_reason == "tool_calls":
        if not message.tool_calls or message.refusal:
            raise FoundryModelError("invalid model tool response")

        # A provider may say "I'll calculate that" alongside its function calls.
        # The calls determine the next loop step; that accompanying text is
        # omitted from our answer field so the coordinator continues execution.
        return ModelReply(calls=tuple(_decode_call(call) for call in message.tool_calls))

    if message.tool_calls:
        raise FoundryModelError("model stop response contains tool calls")

    # A completed refusal is a user-facing response and ends this turn too.
    answer = message.refusal or message.content

    if answer is None or not answer.strip():
        raise FoundryModelError("model response has no answer")

    return ModelReply(answer=answer)


def _decode_call(call: ChatCompletionMessageToolCallUnion) -> ToolCall:
    if call.type != "function":
        raise FoundryModelError("unsupported model tool call")

    if not call.id or not call.function.name:
        raise FoundryModelError("model tool call is missing an identifier")

    # Example: arguments='{"count": 2}' becomes a frozen ToolCall mapping.
    # Object shape and finite JSON values are checked here; the gateway then
    # validates the tool-specific schema and permissions before dispatch.
    try:
        arguments = json.loads(call.function.arguments, object_pairs_hook=_unique_keys)
        return ToolCall(call.id, call.function.name, arguments)
    except TypeError, ValueError:
        raise FoundryModelError("invalid model tool arguments") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:

    # A model response containing {"count": 2, "count": 9} is ambiguous.
    # Reject it before JSON parsing silently keeps only the second value.
    result: dict[str, object] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate argument key")

        result[key] = value

    return result
