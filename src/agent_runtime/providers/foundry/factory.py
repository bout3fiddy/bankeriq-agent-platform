"""Construct Foundry adapters only when startup selects them."""

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from ...coordinator.model import ModelClient, ReasoningEffort
from ...hosting.endpoint import RuntimeEndpoint
from ..hosts import Authenticator, RuntimeServer
from ..models import ModelSettings


@contextmanager
def open_foundry_model(
    settings: ModelSettings, *, environment: Mapping[str, str]
) -> Iterator[ModelClient]:
    """Resolve Azure configuration, validate requested features and own the SDK connection."""

    # For example, FOUNDRY_REASONING_EFFORTS=low,high declares the settings
    # verified for this deployment with function calling. A selected HIGH must
    # be listed here before any credentials or network client are used.
    efforts = frozenset(
        ReasoningEffort(value.strip())
        for value in environment.get("FOUNDRY_REASONING_EFFORTS", "").split(",")
        if value.strip()
    )

    if settings.reasoning_effort is not None and settings.reasoning_effort not in efforts:
        raise ValueError("reasoning effort is unsupported by this deployment")

    timeout = float(environment.get("FOUNDRY_TIMEOUT_SECONDS", "60"))

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Foundry timeout must be finite and positive")

    # SDK imports are deliberately lazy. Registering Foundry as an available
    # choice must work in an installation that uses another provider's SDK.
    from .connection import create_foundry_client
    from .model import FoundryModelClient

    with create_foundry_client(
        base_url=environment["FOUNDRY_OPENAI_BASE_URL"],
        api_key=environment["AZURE_OPENAI_API_KEY"],
        timeout_seconds=timeout,
    ) as client:
        yield FoundryModelClient(
            client,
            deployment=settings.model,
            instructions=settings.instructions,
            supported_reasoning_efforts=efforts,
            max_completion_tokens=settings.max_output_tokens,
        )


def create_foundry_host(endpoint: RuntimeEndpoint, authenticate: Authenticator) -> RuntimeServer:
    """Load the Microsoft hosting SDK independently of the chosen model provider."""

    from .hosting import create_hosted_server

    return create_hosted_server(endpoint, authenticate=authenticate)
