"""Select a model implementation without importing a provider SDK."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.coordinator.model import ModelClient, ReasoningEffort


class ModelSettings(BaseModel):
    """Validated startup choices shared by model adapters; credentials stay outside."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    instructions: str = Field(min_length=1, repr=False)
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int = Field(default=4096, gt=0)


# A factory opens a client and closes its connections when the application exits:
#
# @contextmanager
# def local_model(settings: ModelSettings):
#     yield ScriptedModel()
#
# Register {"local": local_model}; settings.provider="local" selects that factory.
type ModelFactory = Callable[[ModelSettings], AbstractContextManager[ModelClient]]


def open_model(
    settings: ModelSettings, *, providers: Mapping[str, ModelFactory]
) -> AbstractContextManager[ModelClient]:
    """Open the explicitly selected provider; its factory owns credentials and cleanup."""

    factory = providers.get(settings.provider)

    if factory is None:
        raise ValueError("unsupported model provider")

    # A failure propagates to startup. Automatic fallback could send conversation
    # data to a different service than the one approved in deployment configuration.
    return factory(settings)
