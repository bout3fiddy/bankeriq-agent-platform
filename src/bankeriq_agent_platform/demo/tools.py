"""Bounded synthetic tools and an in-memory job completion provider."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from agent_runtime.policy.output import EnforcedOutput
from agent_runtime.runs.context import RunContext
from agent_runtime.storage.memory import InMemoryStateStore
from agent_runtime.tools.bindings import ExecutionOutput, ToolBinding
from agent_runtime.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry
from agent_runtime.tools.results import PendingToolCall, RestrictedReceipt, WaitReason
from agent_runtime.types.json_value import JsonValue

PRIVATE_MARKER = "SYNTHETIC_RESTRICTED_REPORT_CONTENT"
PERMISSIONS = frozenset({"demo.use"})
SAMPLE = ToolDefinition(
    "sample_values",
    "v1",
    "Load the synthetic sample values for the calculation.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    OutputPolicy.VISIBLE,
    required_permissions=PERMISSIONS,
)
CALCULATE = ToolDefinition(
    "calculate",
    "v1",
    "Sum the values returned by sample_values.",
    {
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 16}
        },
        "required": ["values"],
        "additionalProperties": False,
    },
    OutputPolicy.VISIBLE,
    required_permissions=PERMISSIONS,
)
REPORT = ToolDefinition(
    "build_report",
    "v1",
    "Prepare a synthetic restricted report using the calculated total.",
    {
        "type": "object",
        "properties": {"total": {"type": "integer"}},
        "required": ["total"],
        "additionalProperties": False,
    },
    OutputPolicy.RESTRICTED,
    required_permissions=PERMISSIONS,
)


class DemoAccess:
    """Grant the fixed demo identity access to the three synthetic tools."""

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        return (
            PERMISSIONS
            if (context.tenant_id, context.user_id) == ("demo", "local")
            else frozenset()
        )

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        return bool(self.permissions_for(context))

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:

        # These demo tools require no approval. Adding an approval-gated tool
        # leaves it denied until an actual approval provider is supplied.
        return False


@dataclass(frozen=True)
class DeferredResult:
    call: ToolCall
    version: str
    output: EnforcedOutput


class DemoJobs:
    """Hold an enforced report receipt until the matching resume notification."""

    def __init__(self, gateway: ToolGateway, registry: ToolRegistry, access: DemoAccess) -> None:
        self.gateway = gateway
        self.registry = registry
        self.access = access
        self.pending: dict[tuple[RunContext, str], DeferredResult] = {}

    def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
        output = self.gateway.dispatch(call, version=version, context=context)

        if isinstance(output.model_result, RestrictedReceipt):
            reference = str(uuid4())

            # Execution already finished in this fake. Delaying its receipt
            # exercises WAITING -> resume -> another model call without a worker.
            self.pending[(context, reference)] = DeferredResult(call, version, output)
            return EnforcedOutput(PendingToolCall(call.call_id, WaitReason.JOB, reference))

        return output

    def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
        result = self.pending.pop((context, pending.reference_id))
        definition = self.registry.resolve(
            result.call.tool_name, result.version, self.access.permissions_for(context)
        )

        if not self.access.can_access(context, definition, result.call):
            raise PermissionError("demo result unavailable")

        return result.output


class DemoDependencies:
    """Keep mutable storage and fake jobs scoped to one application process."""

    def __init__(self) -> None:
        self.store = InMemoryStateStore()
        self.access = DemoAccess()
        self.registry = ToolRegistry([SAMPLE, CALCULATE, REPORT])
        gateway = ToolGateway(
            self.registry,
            {
                (SAMPLE.name, SAMPLE.version): ToolBinding(sample, validate_empty, public_values),
                (CALCULATE.name, CALCULATE.version): ToolBinding(
                    calculate, validate_values, public_total
                ),
                (REPORT.name, REPORT.version): ToolBinding(report, validate_total),
            },
            self.access,
        )
        self.jobs = DemoJobs(gateway, self.registry, self.access)


def sample(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
    return ExecutionOutput({"values": [12, 30]})


def calculate(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
    return ExecutionOutput({"total": sum(cast(tuple[int, ...], arguments["values"]))})


def report(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:

    # This opaque artifact ID demonstrates delivery metadata. No blob or report
    # service exists in the demo; restricted content must stay out of model input.
    return ExecutionOutput({"body": PRIVATE_MARKER}, artifact_id=f"demo-report-{uuid4()}")


def public_values(payload: JsonValue) -> JsonValue:
    return {"values": cast(Mapping[str, JsonValue], payload)["values"]}


def public_total(payload: JsonValue) -> JsonValue:
    return {"total": cast(Mapping[str, JsonValue], payload)["total"]}


def validate_empty(arguments: Mapping[str, JsonValue]) -> None:
    if arguments:
        raise ValueError("sample_values accepts no arguments")


def validate_values(arguments: Mapping[str, JsonValue]) -> None:
    values = arguments.get("values")

    # JSON arrays arrive as immutable tuples. Bound this public arithmetic tool
    # to small integers, e.g. {"values": [12, 30]} produces {"total": 42}.
    if set(arguments) != {"values"} or not isinstance(values, tuple) or not 1 <= len(values) <= 16:
        raise ValueError("invalid values")

    if any(type(value) is not int or abs(value) > 10_000 for value in values):
        raise ValueError("invalid values")


def validate_total(arguments: Mapping[str, JsonValue]) -> None:
    if set(arguments) != {"total"} or type(arguments["total"]) is not int:
        raise ValueError("invalid total")
