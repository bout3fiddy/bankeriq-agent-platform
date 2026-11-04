"""Bounded provider stream assembly; partial function arguments remain private."""

from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from agent_runtime.providers.foundry.replies import FoundryModelError


class StreamAssembly:
    """Accumulate one finished response without trusting provider chunk boundaries.

    A function can arrive as name="calcu", then name="late", with arguments split
    in the middle of a JSON string. Keep these fragments inside the adapter until
    decode_reply validates the complete JSON. A closed socket without a finish
    reason is a failed attempt, even when some answer text has already appeared.
    """

    def __init__(self) -> None:
        self.identity: tuple[str, str, int] | None = None
        self.content = ""
        self.refusal = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish: str | None = None
        self.usage: Any = None
        self.bytes = 0
        self.chunks = 0

    def add(self, chunk: ChatCompletionChunk) -> str:
        self.chunks += 1
        self.bytes += len(chunk.model_dump_json().encode())

        # Include framing and tool fragments in the bound. For example, a peer
        # sending empty deltas forever still exhausts the finite event budget.
        if self.chunks > 10000 or self.bytes > 2_097_152:
            raise FoundryModelError("model stream exceeds its bound")

        if chunk.usage is not None:
            self.usage = chunk.usage

        if not chunk.choices:
            return ""

        identity = (chunk.id, chunk.model, chunk.created)

        if self.identity is None:
            self.identity = identity
        elif identity != self.identity:
            raise FoundryModelError("model stream identity changed")

        if len(chunk.choices) != 1 or chunk.choices[0].index != 0 or self.finish is not None:
            raise FoundryModelError("invalid model stream choice")

        choice = chunk.choices[0]
        delta = choice.delta

        if delta.audio is not None or delta.function_call is not None:
            raise FoundryModelError("unsupported model stream response")

        if delta.role not in (None, "assistant"):
            raise FoundryModelError("invalid model stream role")

        self.content += delta.content or ""
        self.refusal += delta.refusal or ""

        for fragment in delta.tool_calls or ():
            if fragment.type not in (None, "function"):
                raise FoundryModelError("unsupported streamed tool type")

            if not 0 <= fragment.index < 64:
                raise FoundryModelError("model tool stream exceeds its bound")

            call = self.calls.setdefault(
                fragment.index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            call["id"] += fragment.id or ""

            if fragment.function is not None:
                call["function"]["name"] += fragment.function.name or ""
                call["function"]["arguments"] += fragment.function.arguments or ""

            if len(call["id"]) > 256 or len(call["function"]["name"]) > 256:
                raise FoundryModelError("model tool identity exceeds its bound")

        if len(self.content.encode()) + len(self.refusal.encode()) > 131072:
            raise FoundryModelError("model text exceeds its bound")

        self.finish = choice.finish_reason

        # Refusals and tool commentary are never dispatched as tool arguments.
        # Earlier text can become obsolete when a later chunk selects tools;
        # the runtime resets provisional presentation after receiving that reply.
        return (delta.refusal or delta.content or "") if not self.calls else ""

    def response(self) -> ChatCompletion:
        if self.identity is None or self.finish is None:
            raise FoundryModelError("model stream did not complete")

        identifier, model, created = self.identity
        return ChatCompletion.model_validate(
            {
                "id": identifier,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "usage": self.usage.model_dump() if self.usage is not None else None,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": self.finish,
                        "message": {
                            "role": "assistant",
                            "content": self.content or None,
                            "refusal": self.refusal or None,
                            "tool_calls": [self.calls[index] for index in sorted(self.calls)]
                            or None,
                        },
                    }
                ],
            },
            strict=True,
        )


async def collect_stream(
    stream: AsyncStream[ChatCompletionChunk],
    on_text: Callable[[str], Awaitable[None]],
) -> ChatCompletion:
    assembly = StreamAssembly()

    # The context closes transport resources on completion, cancellation, policy
    # rejection or malformed data. Missing usage stays absent when disconnected;
    # it must never be reported as a zero-token successful request.
    async with stream:
        async for raw in stream:
            data = raw.model_dump(warnings=False)

            # Azure precedes completion chunks with prompt-filter metadata whose
            # object/id/model are empty. It carries no choice or usable answer.
            # Normalize only that documented envelope, then retain strict chunk
            # validation and the shared byte/event limits. A malformed completion
            # with text or a tool call cannot enter through this exception.
            if (
                data.get("object") == ""
                and data.get("id") == ""
                and data.get("model") == ""
                and data.get("created") == 0
                and data.get("choices") == []
                and data.get("usage") is None
                and any(
                    isinstance(data.get(name), list)
                    for name in ("prompt_annotations", "prompt_filter_results")
                )
            ):
                data["object"] = "chat.completion.chunk"

            chunk = ChatCompletionChunk.model_validate(data, strict=True)
            text = assembly.add(chunk)

            if text:
                await on_text(text)

    return assembly.response()
