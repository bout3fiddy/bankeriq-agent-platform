"""Real SDK serialization and response parsing through an in-process HTTP transport."""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import httpx2
import pytest
from openai import OpenAI

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.model import ModelRequest, ModelTool, ReasoningEffort
from agent_runtime.providers.foundry.connection import create_foundry_client
from agent_runtime.providers.foundry.model import FoundryModelClient
from agent_runtime.providers.foundry.replies import FoundryModelError
from agent_runtime.tools.definitions import ToolCall

REQUEST = ModelRequest((Message(MessageRole.USER, "Hello"),), ())


def completion(
    *, finish_reason: str = "stop", content: str | None = "Hello", **message_fields: object
) -> dict[str, object]:
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-deployment",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, **message_fields},
            }
        ],
    }


def function_call(arguments: str = '{"count": 2}', *, call_id: str = "a") -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "calculate", "arguments": arguments},
    }


@contextmanager
def connected_model(
    body: object,
    *,
    status: int = 200,
    efforts: frozenset[ReasoningEffort] = frozenset(),
) -> Iterator[tuple[FoundryModelClient, list[httpx2.Request]]]:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status, json=body)

    with OpenAI(
        base_url="https://synthetic.openai.azure.com/openai/v1/",
        api_key="synthetic-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as client:
        yield (
            FoundryModelClient(
                client,
                deployment="coordinator-deployment",
                supported_reasoning_efforts=efforts,
                max_completion_tokens=2048,
            ),
            requests,
        )


def test_real_sdk_sends_discovered_function_schema_and_returns_answer() -> None:
    call = ToolCall("schema-snapshot", "unused", {"type": "object", "properties": {}})
    request = ModelRequest(
        REQUEST.history,
        (ModelTool("calculate", "Total", call.arguments),),
        instructions="Use permitted results to answer.",
    )

    with connected_model(completion()) as (model, requests):
        reply = model.complete(request)

    payload = json.loads(requests[0].content)
    assert reply.answer == "Hello"
    assert not reply.calls
    assert str(requests[0].url) == "https://synthetic.openai.azure.com/openai/v1/chat/completions"
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == "Bearer synthetic-key"
    assert payload["model"] == "coordinator-deployment"
    assert payload["messages"] == [
        {"role": "system", "content": "Use permitted results to answer."},
        {"role": "user", "content": "Hello"},
    ]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "Total",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            },
        }
    ]
    assert payload["tool_choice"] == "auto"
    assert payload["max_completion_tokens"] == 2048
    assert payload["store"] is False
    assert payload["stream"] is False
    assert "reasoning_effort" not in payload


def test_empty_discovery_omits_tools_and_does_not_share_history_between_calls() -> None:
    with connected_model(completion()) as (model, requests):
        model.complete(REQUEST)
        model.complete(ModelRequest((Message(MessageRole.USER, "Different session"),), ()))

    first, second = [json.loads(request.content) for request in requests]
    assert "tools" not in first
    assert "tool_choice" not in first
    assert second["messages"][1:] == [{"role": "user", "content": "Different session"}]


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_configured_reasoning_effort_reaches_the_wire(effort: ReasoningEffort) -> None:
    with connected_model(completion(), efforts=frozenset({effort})) as (model, requests):
        model.complete(ModelRequest(REQUEST.history, (), reasoning_effort=effort))

    assert json.loads(requests[0].content)["reasoning_effort"] == effort.value


def test_unsupported_reasoning_fails_before_network_call() -> None:
    with (
        connected_model(completion()) as (model, requests),
        pytest.raises(FoundryModelError, match="reasoning effort is unsupported"),
    ):
        model.complete(ModelRequest(REQUEST.history, (), reasoning_effort=ReasoningEffort.HIGH))

    assert requests == []


def test_tool_batch_takes_precedence_over_accompanying_commentary() -> None:
    body = completion(
        finish_reason="tool_calls",
        content="I will calculate these.",
        tool_calls=[function_call(), function_call('{"count": 3}', call_id="b")],
    )

    with connected_model(body) as (model, _):
        reply = model.complete(REQUEST)

    assert reply.answer is None
    assert reply.calls == (
        ToolCall("a", "calculate", {"count": 2}),
        ToolCall("b", "calculate", {"count": 3}),
    )


def test_completed_refusal_is_a_final_answer() -> None:
    with connected_model(completion(content=None, refusal="I cannot do that.")) as (model, _):
        assert model.complete(REQUEST).answer == "I cannot do that."


@pytest.mark.parametrize(
    "arguments",
    [
        "broken PRIVATE_MARKER",
        "[]",
        "null",
        "2",
        '{"count": NaN}',
        '{"count": Infinity}',
        '{"count": 1e400}',
        '{"count": 1, "count": 2}',
    ],
)
def test_invalid_arguments_fail_without_copying_raw_text(arguments: str) -> None:
    body = completion(finish_reason="tool_calls", tool_calls=[function_call(arguments)])

    with (
        connected_model(body) as (model, _),
        pytest.raises(FoundryModelError, match="invalid model tool arguments") as error,
    ):
        model.complete(REQUEST)

    assert "PRIVATE_MARKER" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        completion(finish_reason="length"),
        completion(finish_reason="content_filter"),
        completion(finish_reason="function_call"),
        completion(finish_reason="tool_calls", tool_calls=[]),
        completion(tool_calls=[function_call()]),
        completion(content=" "),
        completion(content=None),
        completion(finish_reason="tool_calls", tool_calls=[function_call(call_id="")]),
        completion(finish_reason="tool_calls", tool_calls=[{"type": "unexpected"}]),
        {"id": "invalid", "choices": []},
        {**completion(), "choices": []},
        {**completion(), "choices": "PRIVATE_MARKER"},
    ],
)
def test_unfinished_or_malformed_responses_cannot_become_answers(body: object) -> None:
    with connected_model(body) as (model, _), pytest.raises(FoundryModelError) as error:
        model.complete(REQUEST)

    assert "PRIVATE_MARKER" not in str(error.value)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_errors_are_sanitised_and_are_not_retried(status: int) -> None:
    with (
        connected_model({"error": {"message": "PRIVATE_MARKER"}}, status=status) as (
            model,
            requests,
        ),
        pytest.raises(FoundryModelError, match="Foundry inference request failed") as error,
    ):
        model.complete(REQUEST)

    assert len(requests) == 1
    assert "PRIVATE_MARKER" not in str(error.value)


def test_connection_creation_is_lazy_and_has_a_bounded_timeout() -> None:
    def token_provider() -> str:
        raise AssertionError("credentials should only be read for an API call")

    with create_foundry_client(
        base_url="https://synthetic.openai.azure.com/openai/v1/", api_key=token_provider
    ) as client:
        assert client.max_retries == 0
        assert client.timeout == 60


def test_shared_client_keeps_concurrent_request_instructions_separate() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    payloads: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payloads.append(json.loads(request.content))
        barrier.wait(timeout=5)
        return httpx2.Response(200, json=completion())

    with OpenAI(
        api_key="synthetic", http_client=httpx2.Client(transport=httpx2.MockTransport(respond))
    ) as client:
        model = FoundryModelClient(client, deployment="synthetic")

        def infer(label: str) -> None:
            request = ModelRequest(
                (Message(MessageRole.USER, label),), (), instructions=f"Instructions {label}"
            )
            model.complete(request)

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(infer, ("session-a", "session-b")))

    assert len(payloads) == 2

    for payload in payloads:
        system, message = payload["messages"]
        assert system == {"role": "system", "content": f"Instructions {message['content']}"}
