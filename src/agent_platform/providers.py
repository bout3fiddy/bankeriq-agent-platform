"""Application-approved provider registrations; constructing this list loads no SDKs."""

from collections.abc import Mapping
from functools import partial

from agent_runtime.providers.foundry.factory import create_foundry_host, open_foundry_model
from agent_runtime.providers.hosts import HostFactory
from agent_runtime.providers.models import ModelFactory


def model_providers(environment: Mapping[str, str]) -> dict[str, ModelFactory]:
    """Bind provider-specific configuration outside the coordinator."""

    # partial(open_foundry_model, environment=environment) creates a callable
    # accepting just ModelSettings. For example, factories["foundry"](settings)
    # later returns the client's context manager with this startup environment
    # already bound. Building this mapping opens no connection or credential flow.
    # A new provider adds its factory here once. MODEL_PROVIDER then chooses it
    # without editing the coordinator, registry, gateway or conversation store.
    return {"foundry": partial(open_foundry_model, environment=environment)}


def hosting_providers() -> dict[str, HostFactory]:
    """List hosting integrations independently of model integrations."""

    return {"foundry": create_foundry_host}
