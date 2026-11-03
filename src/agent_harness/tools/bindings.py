"""Application-supplied execution and validation bindings for registered tools."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from agent_harness.policy.output import VisibleOutputFilter
from agent_harness.runs.context import RunContext
from agent_harness.tools.definitions import ToolCall
from agent_harness.types.json_value import JsonValue


@dataclass(frozen=True, slots=True)
class ExecutionOutput:
    """Internal handler result awaiting output enforcement."""

    payload: JsonValue = field(default=None, repr=False)
    artifact_id: str | None = None


# Example application handler:
# def total(context, arguments):
#     return ExecutionOutput({"total": sum(arguments["values"])})
#
# Pass handler=total in ToolBinding(...), alongside an argument validator and
# public-output filter. Calling it with {"values": [12, 30]} returns total 42;
# the gateway still applies that filter before the value enters model history.
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

    # A durable payment adapter receives the original call and operation identity:
    # lambda context, call, operation_id: provider.pay(call.arguments, key=operation_id).
    # Recovery reuses that key after a timeout. The ordinary handler remains useful
    # for pure calculations and for the synchronous in-memory profile.
    durable_handler: Callable[[RunContext, ToolCall, str], ExecutionOutput] | None = None
