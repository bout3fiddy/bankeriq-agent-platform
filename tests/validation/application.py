"""Assemble the development application using the shared startup composition."""

import hmac
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from starlette.requests import Request

from agent_runtime.coordinator.control import RunLimits
from agent_runtime.hosting.access import AuthenticationRequired, InputRejected, Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.providers.hosts import Authenticator
from bankeriq_agent_platform.configuration import load_settings
from bankeriq_agent_platform.providers import model_providers
from bankeriq_agent_platform.runtime import open_runtime
from bankeriq_agent_platform.server import LocalServer

from .model import open_scripted_model
from .tools import ValidationDependencies

# Requests are test fixtures. No validation role or expected tool sequence is
# supplied as system guidance; application startup selects the real prompt catalog.
PROMPT = json.loads((Path(__file__).parent / "fixtures/request.json").read_text())["message"]
PRINCIPAL = Principal("validation", "local")


@contextmanager
def open_validation(
    environment: Mapping[str, str],
    *,
    provider: str,
    token: str,
    port: int = 8088,
    host: str = "127.0.0.1",
) -> Iterator[tuple[LocalServer, ValidationDependencies]]:
    """Supply explicit fakes while reusing normal model selection and client cleanup."""

    if not token:
        raise ValueError("BANKERIQ_VALIDATION_TOKEN is required")

    # Use the same system/runtime prompt composition as application startup.
    # Reject a test-only override so live validation cannot quietly run different
    # instructions. Scoring and the mock model's scripted progression stay here.
    if "MODEL_INSTRUCTIONS" in environment:
        raise ValueError("validation requires the application prompt catalog")

    configuration = dict(environment)
    configuration.update(MODEL_PROVIDER=provider, HOSTING_PROVIDER="foundry")

    if provider == "mock":
        configuration["MODEL_NAME"] = "synthetic"

    settings = load_settings(configuration)
    assert settings.prompts is not None
    dependencies = ValidationDependencies(settings.prompts)
    models = model_providers(configuration)
    models["mock"] = open_scripted_model

    async def authenticate(request: Request) -> Principal:

        # The shared token maps to one synthetic identity. Tenant/user fields in
        # request bodies remain forbidden by the normal request parser.
        expected = f"Bearer {token}".encode()
        supplied = request.headers.get("authorization", "").encode()

        if not hmac.compare_digest(supplied, expected):
            raise AuthenticationRequired("validation token required")

        return PRINCIPAL

    def create_host(endpoint: RuntimeEndpoint, auth: Authenticator) -> LocalServer:
        return LocalServer(endpoint, auth, port, host)

    with open_runtime(
        settings,
        environment=configuration,
        store=dependencies.store,
        registry=dependencies.registry,
        gateway=dependencies.jobs,
        permissions_for=dependencies.access.permissions_for,
        continuation_resolver=dependencies.jobs,
        authenticate=authenticate,
        prepare_input=admit_validation_input,
        limits=RunLimits(model_steps=6, tool_calls=6),
        models=models,
        hosts={"foundry": create_host},
    ) as server:
        yield cast(LocalServer, server), dependencies


def admit_validation_input(principal: Principal, text: str) -> str:

    # This executable accepts the documented synthetic prompt only. For example,
    # arbitrary pasted customer text fails before any model request is made.
    if principal != PRINCIPAL or text != PROMPT:
        raise InputRejected("use the validation request")

    return text
