"""Execution limits and application-facing coordinator outcomes."""

from dataclasses import dataclass, field

from ..runs.state import RunState
from ..tools.results import ArtifactReference


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Maximum model steps and requested tool calls across a run and its continuations."""

    model_steps: int = 8
    tool_calls: int = 16

    def __post_init__(self) -> None:

        # Zero tool calls permits a text-only run. At least one model step is
        # needed to answer; negative budgets indicate invalid configuration.
        if self.model_steps < 1 or self.tool_calls < 0:
            raise ValueError("model_steps must be positive and tool_calls nonnegative")


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    """Run outcome and artifact references for the authenticated application."""

    run: RunState
    answer: str | None = field(default=None, repr=False)
    artifacts: tuple[ArtifactReference, ...] = ()
