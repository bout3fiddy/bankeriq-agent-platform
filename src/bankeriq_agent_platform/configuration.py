"""Read deployment choices at application startup."""

from collections.abc import Mapping
from dataclasses import dataclass

from agent_runtime.coordinator.model import ReasoningEffort
from agent_runtime.providers.models import ModelSettings


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Choose model access and hosting separately; both initially default to Foundry."""

    model: ModelSettings
    hosting_provider: str = "foundry"


def load_settings(environment: Mapping[str, str]) -> RuntimeSettings:
    """Parse trusted deployment configuration; HTTP requests cannot select providers."""

    # Example: MODEL_NAME="coordinator", MODEL_REASONING_EFFORT="high" and
    # MODEL_MAX_OUTPUT_TOKENS="2048" become a named deployment, HIGH enum and
    # integer limit. Missing MODEL_NAME, an unknown effort or nonnumeric limit
    # fails during startup, before a server can accept a request with bad settings.
    # MODEL_INSTRUCTIONS supplies the fixed resolver when no shared resolver is
    # injected; live prompt publication is configured through application wiring.
    effort = environment.get("MODEL_REASONING_EFFORT")
    return RuntimeSettings(
        model=ModelSettings(
            provider=environment.get("MODEL_PROVIDER", "foundry"),
            model=environment["MODEL_NAME"],
            instructions=environment.get(
                "MODEL_INSTRUCTIONS", "Use available tools and permitted results to help the user."
            ),
            reasoning_effort=ReasoningEffort(effort) if effort else None,
            max_output_tokens=int(environment.get("MODEL_MAX_OUTPUT_TOKENS", "4096")),
        ),
        hosting_provider=environment.get("HOSTING_PROVIDER", "foundry"),
    )
