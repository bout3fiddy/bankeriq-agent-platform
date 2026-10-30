"""Application-supplied execution and validation bindings for registered tools."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..policy.output import VisibleOutputFilter
from ..types.json_value import JsonValue
from ..types.runs import RunContext


@dataclass(frozen=True, slots=True)
class ExecutionOutput:
    """Internal handler result awaiting output enforcement."""

    payload: JsonValue = field(default=None, repr=False)
    artifact_id: str | None = None


# Example handler: total(context, arguments) -> ExecutionOutput({"total": 42}).
# context carries trusted identity separately from model-generated arguments.
type ToolHandler = Callable[[RunContext, Mapping[str, JsonValue]], ExecutionOutput]

# A validator checks the operation's argument schema and domain constraints.
# For example, validate_count({"count": -1}) raises ValueError for a negative count.
# Unexpected validator failures are handled as execution failures by the gateway.
type ArgumentValidator = Callable[[Mapping[str, JsonValue]], None]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """Trusted per-version wiring, supplied by application startup or local tests."""

    handler: ToolHandler
    validate_arguments: ArgumentValidator
    visible_filter: VisibleOutputFilter | None = None
