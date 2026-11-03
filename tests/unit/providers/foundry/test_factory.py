"""Foundry startup maps common model settings and owns its SDK client."""

import json

import httpx2
import pytest
from openai import OpenAI

from agent_harness.coordinator.model import ModelRequest, ReasoningEffort
from agent_runtime.providers.foundry.factory import open_foundry_model
from agent_runtime.providers.models import ModelSettings


def test_factory_maps_settings_and_closes_the_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    clients: list[OpenAI] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello"},
                    }
                ],
            },
        )

    def connect(*, base_url: str, api_key: str, timeout_seconds: float) -> OpenAI:
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("agent_runtime.providers.foundry.connection.create_foundry_client", connect)
    settings = ModelSettings(
        provider="foundry",
        model="coordinator-deployment",
        instructions="Help",
        reasoning_effort=ReasoningEffort.HIGH,
        max_output_tokens=2048,
    )
    environment = {
        "FOUNDRY_OPENAI_BASE_URL": "https://synthetic.openai.azure.com/openai/v1/",
        "AZURE_OPENAI_API_KEY": "synthetic-key",
        "FOUNDRY_REASONING_EFFORTS": "low, high",
        "FOUNDRY_TIMEOUT_SECONDS": "12",
    }

    with open_foundry_model(settings, environment=environment) as model:
        reply = model.complete(ModelRequest((), (), reasoning_effort=settings.reasoning_effort))
        assert reply.answer == "Hello"
        assert not clients[0].is_closed()

    assert clients[0].is_closed()
    assert clients[0].timeout == 12
    payload = json.loads(requests[0].content)
    assert payload["model"] == "coordinator-deployment"
    assert payload["reasoning_effort"] == "high"
    assert payload["max_completion_tokens"] == 2048


def test_unsupported_feature_fails_before_credentials_are_required() -> None:
    settings = ModelSettings(
        provider="foundry",
        model="deployment",
        instructions="Help",
        reasoning_effort=ReasoningEffort.HIGH,
    )

    with (
        pytest.raises(ValueError, match="reasoning effort is unsupported"),
        open_foundry_model(settings, environment={}),
    ):
        pytest.fail("unsupported feature must fail startup")


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_is_rejected_before_opening_a_connection(timeout: str) -> None:
    settings = ModelSettings(provider="foundry", model="deployment", instructions="Help")

    with (
        pytest.raises(ValueError, match="timeout must be finite and positive"),
        open_foundry_model(settings, environment={"FOUNDRY_TIMEOUT_SECONDS": timeout}),
    ):
        pytest.fail("invalid timeout must fail startup")
