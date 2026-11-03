"""Trusted startup selection and provider resource lifetime."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from agent_harness.coordinator.model import ModelClient, ModelReply, ModelRequest, ReasoningEffort
from agent_platform.configuration import load_settings
from agent_platform.providers import hosting_providers, model_providers
from agent_runtime.providers.hosts import select_host
from agent_runtime.providers.models import ModelSettings, open_model


class LocalModel:
    def complete(self, request: ModelRequest) -> ModelReply:
        return ModelReply(answer="Local answer")


def test_default_configuration_selects_foundry_without_reading_azure_credentials() -> None:
    settings = load_settings({"MODEL_NAME": "coordinator"})

    assert settings.model.provider == "foundry"
    assert settings.model.model == "coordinator"
    assert settings.hosting_provider == "foundry"
    assert set(model_providers({})) == {"foundry"}
    assert set(hosting_providers()) == {"foundry"}


def test_model_and_hosting_choices_are_independent() -> None:
    settings = load_settings(
        {
            "MODEL_PROVIDER": "local-model",
            "MODEL_NAME": "test-model",
            "HOSTING_PROVIDER": "foundry",
            "MODEL_REASONING_EFFORT": "high",
            "MODEL_MAX_OUTPUT_TOKENS": "2048",
        }
    )

    assert settings.model.provider == "local-model"
    assert settings.hosting_provider == "foundry"
    assert settings.model.reasoning_effort is ReasoningEffort.HIGH
    assert settings.model.max_output_tokens == 2048


def test_unknown_providers_fail_without_fallback() -> None:
    settings = ModelSettings(provider="unknown", model="model", instructions="Help")

    with pytest.raises(ValueError, match="unsupported model provider"):
        open_model(settings, providers={})

    with pytest.raises(ValueError, match="unsupported hosting provider"):
        select_host("unknown", providers={})


def test_selected_provider_failure_does_not_try_another_factory() -> None:
    opened: list[str] = []

    @contextmanager
    def broken(settings: ModelSettings) -> Iterator[ModelClient]:
        opened.append("broken")
        raise RuntimeError("provider unavailable")
        yield LocalModel()

    @contextmanager
    def alternative(settings: ModelSettings) -> Iterator[ModelClient]:
        opened.append("alternative")
        yield LocalModel()

    settings = ModelSettings(provider="broken", model="model", instructions="Help")

    with (
        pytest.raises(RuntimeError, match="provider unavailable"),
        open_model(settings, providers={"broken": broken, "alternative": alternative}),
    ):
        pytest.fail("unavailable model must fail startup")

    assert opened == ["broken"]


def test_connections_close_when_application_body_raises() -> None:
    closed: list[bool] = []

    @contextmanager
    def factory(settings: ModelSettings) -> Iterator[ModelClient]:
        try:
            yield LocalModel()
        finally:
            closed.append(True)

    settings = ModelSettings(provider="local", model="model", instructions="Help")

    with (
        pytest.raises(RuntimeError, match="server failure"),
        open_model(settings, providers={"local": factory}) as model,
    ):
        assert model.complete(ModelRequest((), ())).answer == "Local answer"
        raise RuntimeError("server failure")

    assert closed == [True]


@pytest.mark.parametrize(
    "overrides",
    [
        {"MODEL_REASONING_EFFORT": "turbo"},
        {"MODEL_MAX_OUTPUT_TOKENS": "zero"},
        {"MODEL_MAX_OUTPUT_TOKENS": "0"},
        {"MODEL_PROVIDER": ""},
        {"MODEL_NAME": ""},
    ],
)
def test_invalid_startup_model_settings_fail_early(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_settings({"MODEL_NAME": "coordinator", **overrides})
