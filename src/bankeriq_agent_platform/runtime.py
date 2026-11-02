"""Compose the runtime and selected providers at application startup."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from agent_runtime.coordinator.control import RunLimits
from agent_runtime.coordinator.runner import Coordinator, ToolDispatcher
from agent_runtime.durable.continuation import ContinuationResolver
from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.hosting.access import Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.providers.hosts import Authenticator, HostFactory, RuntimeServer, select_host
from agent_runtime.providers.models import ModelFactory, open_model
from agent_runtime.runs.context import RunContext
from agent_runtime.storage.interface import StateStore
from agent_runtime.tools.registry import ToolRegistry

from .configuration import RuntimeSettings
from .providers import hosting_providers, model_providers


@contextmanager
def open_runtime(
    settings: RuntimeSettings,
    *,
    environment: Mapping[str, str],
    store: StateStore,
    registry: ToolRegistry,
    gateway: ToolDispatcher,
    permissions_for: Callable[[RunContext], frozenset[str]],
    continuation_resolver: ContinuationResolver,
    authenticate: Authenticator,
    prepare_input: Callable[[Principal, str], str],
    limits: RunLimits | None = None,
    models: Mapping[str, ModelFactory] | None = None,
    hosts: Mapping[str, HostFactory] | None = None,
) -> Iterator[RuntimeServer]:
    """Keep the selected model alive for the server's lifetime, including resumed runs."""

    # Resolve choices once at startup. Every turn and resumed job in this runtime
    # uses that model client. Request bodies cannot replace these registrations.
    available_models = model_providers(environment) if models is None else models
    available_hosts = hosting_providers() if hosts is None else hosts
    create_host = select_host(settings.hosting_provider, providers=available_hosts)

    # Example: with open_runtime(settings, ...) as server: server.run().
    # The model factory closes its SDK connections when this block exits, even
    # when server construction or execution raises an exception.
    with open_model(settings.model, providers=available_models) as model:
        coordinator = Coordinator(
            store,
            registry,
            gateway,
            model,
            permissions_for,
            limits,
            reasoning_effort=settings.model.reasoning_effort,
        )
        continuations = ContinuationHandler(store, coordinator, continuation_resolver)
        endpoint = RuntimeEndpoint(store, coordinator, continuations, prepare_input=prepare_input)
        yield create_host(endpoint, authenticate)
