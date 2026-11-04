"""Real SDK streaming boundaries with synthetic provider frames and fragmented calls."""

import asyncio
import json
from dataclasses import replace

import httpx2
import pytest
from openai import AsyncOpenAI

from agent_harness.coordinator.model import ModelRateLimited, ModelRequest
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.providers.foundry.replies import FoundryModelError
from agent_runtime.providers.foundry.streaming import StreamAssembly


def frame(delta=None, finish=None, *, usage=None, index=0):
    return {
        "id": "synthetic",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture",
        "choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}]
        if usage is None
        else [],
        "usage": usage,
    }


def stream_model(frames, *, request=None, status=200, callback=None, observations=None):
    text = []
    requests = []
    observations = [] if observations is None else observations

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            status,
            content="".join("data: " + json.dumps(value) + "\n\n" for value in frames)
            + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream", "retry-after": "1"},
        )

    async def run():
        async def on_text(value):
            text.append(value)

            if callback:
                await callback(value)

        async with AsyncOpenAI(
            api_key="synthetic",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            model = AsyncFoundryModelClient(
                client, deployment="fixture", observer=observations.append
            )
            reply = await model.stream(request or ModelRequest((), ()), on_text)
            return reply, text, requests, observations

    return asyncio.run(run())


def test_text_and_usage_are_assembled_without_changing_complete_reply():
    reply, text, requests, observations = stream_model(
        [
            frame({"role": "assistant", "content": "Hello "}),
            frame({"content": "world"}),
            frame(finish="stop"),
            frame(usage={"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}),
        ]
    )
    assert reply.answer == "Hello world"
    assert text == ["Hello ", "world"]
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert observations[0].input_tokens == 12
    assert observations[0].output_tokens == 2


@pytest.mark.parametrize("field", ["prompt_annotations", "prompt_filter_results"])
def test_azure_prompt_filter_envelope_precedes_strict_completion_chunks(field):
    annotation = {
        "id": "",
        "object": "",
        "created": 0,
        "model": "",
        "choices": [],
        "usage": None,
        field: [{"prompt_index": 0, "content_filter_results": {}}],
    }
    reply, text, _, observations = stream_model(
        [annotation, frame({"content": "OK"}, finish="stop")]
    )
    assert reply.answer == "OK"
    assert text == ["OK"]
    assert len(observations) == 1

    # Metadata alone cannot complete a response; an invalid answer envelope
    # cannot bypass strict validation by supplying the annotation field.
    with pytest.raises(FoundryModelError):
        stream_model([annotation])

    annotation["choices"] = frame({"content": "injected"}, finish="stop")["choices"]

    with pytest.raises(FoundryModelError):
        stream_model([annotation])


def test_failed_stream_records_one_attempt_with_unknown_usage():
    observations = []

    with pytest.raises(FoundryModelError):
        stream_model([frame({"content": "partial"})], observations=observations)

    assert len(observations) == 1
    assert observations[0].status_code == 200
    assert observations[0].input_tokens is None
    assert observations[0].output_tokens is None


def test_interleaved_repeated_tools_only_dispatch_complete_argument_objects():
    def call(index, **fields):
        return frame({"tool_calls": [{"index": index, **fields}]})

    reply, text, _, _ = stream_model(
        [
            call(0, id="first", type="function", function={"name": "calculate", "arguments": "{"}),
            call(1, id="second", type="function", function={"name": "calculate", "arguments": "{"}),
            call(0, function={"arguments": '"values":[2,3]}'}),
            call(1, function={"arguments": '"values":[4,5]}'}),
            frame(finish="tool_calls"),
        ]
    )
    assert text == []
    assert [call.call_id for call in reply.calls] == ["first", "second"]
    assert [call.arguments["values"] for call in reply.calls] == [(2, 3), (4, 5)]


@pytest.mark.parametrize(
    "frames",
    [
        [frame({"content": "partial"})],
        [frame({"content": "partial"}, finish="length")],
        [frame({"content": "filtered"}, finish="content_filter")],
        [frame({"content": "wrong choice"}, finish="stop", index=1)],
        [frame({"content": "done"}, finish="stop"), frame({"content": "late"})],
        [frame({"role": "system", "content": "invalid"}, finish="stop")],
        [frame({"content": "x" * 131073}, finish="stop")],
        [frame({"tool_calls": [{"index": 65}]}, finish="tool_calls")],
        [frame({"tool_calls": [{"index": 0, "type": "unsupported"}]}, finish="tool_calls")],
    ],
)
def test_invalid_or_truncated_stream_cannot_be_a_completed_answer(frames):
    with pytest.raises(FoundryModelError):
        stream_model(frames)


def test_missing_usage_is_unknown_and_rejection_remains_a_durable_retry_signal():
    _, _, _, observations = stream_model([frame({"content": "hello"}, finish="stop")])
    assert observations[0].output_tokens is None

    with pytest.raises(ModelRateLimited) as error:
        stream_model([], status=429)

    assert error.value.retry_after == 1


def test_cancelled_callback_propagates_without_retrying_provider():
    observations = []

    async def cancelled(text):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        stream_model(
            [frame({"content": "partial"}), frame(finish="stop")],
            callback=cancelled,
            observations=observations,
        )

    assert len(observations) == 1
    assert observations[0].input_tokens is None


def test_structured_output_is_buffered_and_never_published_as_text():
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                    }
                ],
            },
        )

    async def run():
        async def forbidden(text):
            raise AssertionError("structured output became provisional text")

        async with AsyncOpenAI(
            api_key="synthetic",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            model = AsyncFoundryModelClient(client, deployment="fixture")
            return await model.stream(
                replace(ModelRequest((), ()), output_schema={"type": "object"}), forbidden
            )

    assert asyncio.run(run()).answer == '{"ok":true}'
    assert calls[0]["stream"] is False


def test_empty_chunk_flood_has_a_finite_event_bound():
    from openai.types.chat import ChatCompletionChunk

    assembly = StreamAssembly()
    value = ChatCompletionChunk.model_validate(frame())
    assembly.chunks = 10000

    with pytest.raises(FoundryModelError, match="bound"):
        assembly.add(value)
