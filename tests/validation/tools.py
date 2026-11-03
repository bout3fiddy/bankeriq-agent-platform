"""Bounded synthetic tools and an in-memory job completion provider."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from agent_harness.policy.output import EnforcedOutput
from agent_harness.prompts.resolution import PromptResolver
from agent_harness.runs.context import RunContext
from agent_harness.tools.bindings import ExecutionOutput, ToolBinding
from agent_harness.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_harness.tools.gateway import ToolGateway
from agent_harness.tools.registry import ToolRegistry
from agent_harness.tools.results import PendingToolCall, RestrictedReceipt, WaitReason
from agent_harness.types.json_value import JsonValue
from agent_runtime.storage.memory import InMemoryStateStore

PRIVATE_MARKER = "SYNTHETIC_RESTRICTED_REPORT_CONTENT"
PERMISSIONS = frozenset({"validation.use"})
SAMPLE = ToolDefinition(
    "sample_values",
    "v1",
    "Load the available sample values.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    OutputPolicy.VISIBLE,
    required_permissions=PERMISSIONS,
)
CALCULATE = ToolDefinition(
    "calculate",
    "v1",
    "Sum the supplied numeric values.",
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
    "Prepare a restricted report using the supplied total.",
    {
        "type": "object",
        "properties": {"total": {"type": "integer"}},
        "required": ["total"],
        "additionalProperties": False,
    },
    OutputPolicy.RESTRICTED,
    required_permissions=PERMISSIONS,
)


class ValidationAccess:
    """Grant the fixed validation identity access to the three synthetic tools."""

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        return (
            PERMISSIONS
            if (context.tenant_id, context.user_id) == ("validation", "local")
            else frozenset()
        )

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        return bool(self.permissions_for(context))

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:

        # These validation tools require no approval. Adding an approval-gated tool
        # leaves it denied until an actual approval provider is supplied.
        return False


@dataclass(frozen=True)
class DeferredResult:
    call: ToolCall
    version: str
    output: EnforcedOutput


class ValidationJobs:
    """Hold an enforced report receipt until the matching resume notification."""

    def __init__(
        self, gateway: ToolGateway, registry: ToolRegistry, access: ValidationAccess
    ) -> None:
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

        # Example: (context, "job-17") locates the already-enforced report output.
        # pop consumes that fake notification once; a different user/run context
        # cannot retrieve it even if it knows the reference. Resolve the original
        # tool version against current permissions before releasing its receipt
        # and separate delivery reference. Durable replay is outside this fake.
        result = self.pending.pop((context, pending.reference_id))
        definition = self.registry.resolve(
            result.call.tool_name, result.version, self.access.permissions_for(context)
        )

        if not self.access.can_access(context, definition, result.call):
            raise PermissionError("validation result unavailable")

        return result.output


class ValidationDependencies:
    """Keep mutable storage and fake jobs scoped to one application process."""

    def __init__(self, prompts: PromptResolver) -> None:
        self.prompts = prompts
        self.store = InMemoryStateStore()
        self.access = ValidationAccess()
        self.registry = ToolRegistry([SAMPLE, CALCULATE, REPORT])

        # Each binding joins an executor, argument validator and optional visible
        # projection. For example, calculate(context, {"values": (12, 30)}) returns
        # ExecutionOutput({"total": 42}); public_total exposes that approved field.
        # The restricted report binding has no visible projection: output policy
        # produces a completion receipt and routes its artifact ID separately.
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
        self.jobs = ValidationJobs(gateway, self.registry, self.access)


def sample(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
    return ExecutionOutput({"values": [12, 30]})


def calculate(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
    return ExecutionOutput({"total": sum(cast(tuple[int, ...], arguments["values"]))})


def report(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:

    # This opaque artifact ID demonstrates delivery metadata. No blob or report
    # service exists in this fixture; restricted content must stay out of model input.
    return ExecutionOutput({"body": PRIVATE_MARKER}, artifact_id=f"validation-report-{uuid4()}")


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
