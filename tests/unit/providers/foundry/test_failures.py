"""Clean rejection classification and numeric observation over real SDK transports."""

import asyncio
from dataclasses import asdict

import httpx2
import pytest
from openai import AsyncOpenAI

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.model import ModelContextOverflow, ModelRateLimited, ModelRequest
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.providers.foundry.replies import FoundryModelError

REQUEST = ModelRequest((Message(MessageRole.USER, "PRIVATE_REQUEST"),), ())


def completion(content):
    return {
        "id": "local",
        "object": "chat.completion",
        "created": 1,
        "model": "local",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


@pytest.mark.parametrize(
    "status,code,headers,expected,delay",
    [
        (400, "context_length_exceeded", {}, ModelContextOverflow, None),
        (500, "context_length_exceeded", {}, FoundryModelError, None),
        (400, "other", {}, FoundryModelError, None),
        (429, "rate_limit", {"retry-after-ms": "1500"}, ModelRateLimited, 1.5),
        (429, "rate_limit", {"retry-after": "4"}, ModelRateLimited, 4),
        (429, "rate_limit", {"retry-after-ms": "NaN"}, ModelRateLimited, 2),
        (429, "rate_limit", {"retry-after-ms": "Infinity"}, ModelRateLimited, 2),
        (429, "rate_limit", {"retry-after-ms": "600000"}, ModelRateLimited, 300),
    ],
)
def test_classification_is_exact_and_never_sleeps_or_retries(
    status, code, headers, expected, delay
):
    calls, observations = [], []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            status, headers=headers, json={"error": {"code": code, "message": "PRIVATE_MARKER"}}
        )

    async def run():
        async with AsyncOpenAI(
            api_key="PRIVATE_KEY",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            model = AsyncFoundryModelClient(
                client, deployment="local", observer=observations.append
            )

            with pytest.raises(expected) as error:
                await model.complete(REQUEST)

            assert "PRIVATE" not in str(error.value)

            if delay:
                assert error.value.retry_after == delay

    asyncio.run(run())
    assert len(calls) == len(observations) == 1
    assert observations[0].status_code == status
    assert "PRIVATE" not in str(observations)


def test_observations_report_actual_usage_without_payload_or_callback_failure():
    records = []

    def respond(request):
        body = completion(content="PRIVATE_RESPONSE")
        body["usage"] = {"prompt_tokens": 132, "completion_tokens": 8, "total_tokens": 140}
        return httpx2.Response(
            200,
            json=body,
            headers={
                "x-ratelimit-limit-tokens": "50000",
                "x-ratelimit-remaining-tokens": "48000",
                "authorization": "PRIVATE_CREDENTIAL",
            },
        )

    def observe(record):
        records.append(asdict(record))
        raise RuntimeError("sink unavailable")

    async def run():
        async with AsyncOpenAI(
            api_key="PRIVATE_KEY",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            model = AsyncFoundryModelClient(client, deployment="local", observer=observe)
            assert (await model.complete(REQUEST)).answer == "PRIVATE_RESPONSE"

    asyncio.run(run())
    assert len(records) == 1
    assert records[0]["input_tokens"] == 132 and records[0]["output_tokens"] == 8
    assert records[0]["token_limit"] == 50000
    assert "PRIVATE" not in str(records)


def test_malformed_usage_cannot_put_provider_strings_into_numeric_telemetry():
    records = []

    def respond(request):
        body = completion(content="answer")
        body["usage"] = {
            "prompt_tokens": "PRIVATE_MARKER",
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        return httpx2.Response(200, json=body)

    async def run():
        async with AsyncOpenAI(
            api_key="local", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        ) as client:
            model = AsyncFoundryModelClient(client, deployment="local", observer=records.append)

            with pytest.raises(FoundryModelError, match="invalid Foundry response"):
                await model.complete(REQUEST)

    asyncio.run(run())
    assert len(records) == 1
    assert records[0].input_tokens is None
    assert records[0].output_tokens is None
    assert "PRIVATE_MARKER" not in repr(records)
