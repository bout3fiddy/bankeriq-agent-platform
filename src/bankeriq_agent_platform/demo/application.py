"""Assemble the development application using the shared startup composition."""

import hmac
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

from starlette.requests import Request

from agent_runtime.coordinator.control import RunLimits
from agent_runtime.hosting.access import AuthenticationRequired, InputRejected, Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.providers.hosts import Authenticator

from ..configuration import load_settings
from ..prompt_files.loader import load_catalog
from ..providers import model_providers
from ..runtime import open_runtime
from .model import open_demo_model
from .tools import DemoDependencies

# The synthetic user input and instruction sequence are versioned alongside the
# runtime/system definitions. Keep these exports for the admission/transcript
# helpers; both now obtain their exact text from pinned JSON assets.
_DEMO_CATALOG = load_catalog()
PROMPT = _DEMO_CATALOG.compile(_DEMO_CATALOG.catalog.defaults.demo_input).body
INSTRUCTIONS = _DEMO_CATALOG.compile(_DEMO_CATALOG.catalog.defaults.demo).body
PRINCIPAL = Principal("demo", "local")


@dataclass
class LocalServer:
    """Default to loopback; container startup explicitly selects all interfaces."""

    endpoint: RuntimeEndpoint
    authenticate: Authenticator
    port: int
    host: str = "127.0.0.1"

    def run(self) -> None:
        from agent_runtime.providers.foundry.hosting import create_hosted_server

        create_hosted_server(self.endpoint, authenticate=self.authenticate).run(
            host=self.host, port=self.port
        )


@contextmanager
def open_demo(
    environment: Mapping[str, str],
    *,
    provider: str,
    token: str,
    port: int = 8088,
    host: str = "127.0.0.1",
) -> Iterator[tuple[LocalServer, DemoDependencies]]:
    """Supply explicit fakes while reusing normal model selection and client cleanup."""

    if not token:
        raise ValueError("BANKERIQ_DEMO_TOKEN is required")

    # Copy caller configuration before choosing the synthetic prompt and limits.
    # For example, provider="foundry" can retain its endpoint and credentials,
    # while both Foundry and mock executions receive the same three-tool scenario.
    # Mutating this local copy leaves another server's startup mapping unchanged.
    configuration = dict(environment)
    configuration.update(
        MODEL_PROVIDER=provider,
        HOSTING_PROVIDER="foundry",
        MODEL_INSTRUCTIONS=INSTRUCTIONS,
        MODEL_MAX_OUTPUT_TOKENS="256",
    )

    if provider == "mock":
        configuration["MODEL_NAME"] = "synthetic"

    settings = load_settings(configuration)
    dependencies = DemoDependencies()
    models = model_providers(configuration)
    models["mock"] = open_demo_model

    async def authenticate(request: Request) -> Principal:

        # The shared token maps to one synthetic identity. Tenant/user fields in
        # request bodies remain forbidden by the normal request parser.
        expected = f"Bearer {token}".encode()
        supplied = request.headers.get("authorization", "").encode()

        if not hmac.compare_digest(supplied, expected):
            raise AuthenticationRequired("demo token required")

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
        prepare_input=admit_demo_input,
        limits=RunLimits(model_steps=6, tool_calls=6),
        models=models,
        hosts={"foundry": create_host},
    ) as server:
        yield cast(LocalServer, server), dependencies


def admit_demo_input(principal: Principal, text: str) -> str:

    # This executable accepts the documented synthetic prompt only. For example,
    # arbitrary pasted customer text fails before any model request is made.
    if principal != PRINCIPAL or text != PROMPT:
        raise InputRejected("use the synthetic demo prompt")

    return text
