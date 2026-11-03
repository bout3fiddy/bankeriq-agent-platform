"""Hosting selection is independent of the model used by the coordinator."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from starlette.requests import Request

from agent_runtime.hosting.access import Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint


class RuntimeServer(Protocol):
    """Application startup runs the server built by the selected hosting adapter."""

    def run(self) -> None: ...


type Authenticator = Callable[[Request], Awaitable[Principal]]
type HostFactory = Callable[[RuntimeEndpoint, Authenticator], RuntimeServer]


def select_host(provider: str, *, providers: Mapping[str, HostFactory]) -> HostFactory:
    """Select the hosting factory before startup opens any model connections."""

    factory = providers.get(provider)

    if factory is None:
        raise ValueError("unsupported hosting provider")

    return factory
