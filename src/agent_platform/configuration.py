"""Read deployment choices at application startup."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_harness.coordinator.model import ReasoningEffort
from agent_harness.prompts.resolution import FixedPromptResolver, PromptResolver
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.providers.models import ModelSettings


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Choose model access and hosting separately; both initially default to Foundry."""

    model: ModelSettings
    hosting_provider: str = "foundry"
    prompts: PromptResolver | None = None


def load_settings(environment: Mapping[str, str]) -> RuntimeSettings:
    """Parse trusted deployment configuration; HTTP requests cannot select providers."""

    # Example: MODEL_NAME="coordinator", MODEL_REASONING_EFFORT="high" and
    # MODEL_MAX_OUTPUT_TOKENS="2048" become a named deployment, HIGH enum and
    # integer limit. Missing MODEL_NAME, an unknown effort or nonnumeric limit
    # fails during startup, before a server can accept a request with bad settings.
    # MODEL_INSTRUCTIONS supplies the fixed resolver when no shared resolver is
    # injected; live prompt publication is configured through application wiring.
    # Resolve the file profile once at startup. A request cannot replace this
    # catalog or its pinned references. An explicit legacy MODEL_INSTRUCTIONS
    # override still supplies a fixed resolver for existing trusted integrations.
    directory = environment.get("BANKERIQ_PROMPT_DIRECTORY")
    prompts = (
        FixedPromptResolver(environment["MODEL_INSTRUCTIONS"])
        if "MODEL_INSTRUCTIONS" in environment
        else load_catalog(Path(directory) if directory else None).resolver()
    )
    effort = environment.get("MODEL_REASONING_EFFORT")
    return RuntimeSettings(
        model=ModelSettings(
            provider=environment.get("MODEL_PROVIDER", "foundry"),
            model=environment["MODEL_NAME"],
            instructions=prompts.instructions(prompts.resolve()),
            reasoning_effort=ReasoningEffort(effort) if effort else None,
            max_output_tokens=int(environment.get("MODEL_MAX_OUTPUT_TOKENS", "4096")),
        ),
        hosting_provider=environment.get("HOSTING_PROVIDER", "foundry"),
        prompts=prompts,
    )
