"""Wire real runtime components to synthetic model, approval, job and report providers."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import cast

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.control import CoordinatorResult
from agent_runtime.coordinator.model import ModelReply, ModelRequest
from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.durable.continuation import ContinuationEvent
from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.policy.output import EnforcedOutput
from agent_runtime.runs.context import RunContext
from agent_runtime.storage.memory import InMemoryStateStore
from agent_runtime.tools.bindings import ExecutionOutput, ToolBinding
from agent_runtime.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry, ToolUnavailableError
from agent_runtime.tools.results import (
    PendingToolCall,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    WaitReason,
)
from agent_runtime.types.json_value import JsonValue

PRIVATE_BODY = "RESTRICTED_PAYLOAD_SENTINEL"
PERMISSIONS = frozenset({"public.calculate", "reports.create", "reports.read"})
CONTEXT = RunContext("tenant", "user", "chat", "run")
CALCULATE = ToolDefinition(
    "calculate",
    "v1",
    "Sum public integers",
    {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
        "required": ["values"],
        "additionalProperties": False,
    },
    OutputPolicy.VISIBLE,
    required_permissions=frozenset({"public.calculate"}),
)
REPORT = ToolDefinition(
    "build_report",
    "v2",
    "Prepare an internal report from a public total",
    {
        "type": "object",
        "properties": {"dataset": {"type": "string"}, "total": {"type": "integer"}},
        "required": ["dataset", "total"],
        "additionalProperties": False,
    },
    OutputPolicy.RESTRICTED,
    required_permissions=frozenset({"reports.create"}),
    requires_approval=True,
)

type ModelStep = ModelReply | Exception | Callable[[ModelRequest], ModelReply]


@dataclass
class ScriptedModel:
    replies: list[ModelStep] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)
    request_payloads: list[str] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)

        # Inspect actual fields: repr(request) intentionally hides history and payloads.
        self.request_payloads.append(inspect_fields(request))
        reply = self.replies.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return reply(request) if callable(reply) else reply


@dataclass
class Approval:
    call: ToolCall
    version: str
    granted: bool | None = None
    consumed: bool = False


@dataclass
class FakeAccess:
    permissions: dict[tuple[str, str], frozenset[str]] = field(
        default_factory=lambda: {("tenant", "user"): PERMISSIONS}
    )
    datasets: dict[tuple[str, str], frozenset[str]] = field(
        default_factory=lambda: {("tenant", "user"): frozenset({"sample"})}
    )
    approvals: dict[tuple[RunContext, str], Approval] = field(default_factory=dict)

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        return self.permissions.get((context.tenant_id, context.user_id), frozenset())

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        if definition.name == REPORT.name:
            return call.arguments["dataset"] in self.datasets.get(
                (context.tenant_id, context.user_id), frozenset()
            )

        return True

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:
        approval = self.approvals.setdefault(
            (context, call.call_id), Approval(call, definition.version)
        )

        # A decision covers the exact call and selected version within this context.
        # The fake provider keeps decisions in memory and has no expiry scheduler.
        if approval.call != call or approval.version != definition.version:
            return False

        if approval.granted is not True or approval.consumed:
            return False

        approval.consumed = True
        return True


@dataclass(frozen=True)
class DeferredResult:
    call: ToolCall
    version: str
    output: EnforcedOutput


class FakeEvents:
    """Translate fake approval state and delayed gateway outcomes into notifications."""

    def __init__(self, gateway: ToolGateway, registry: ToolRegistry, access: FakeAccess) -> None:
        self.gateway = gateway
        self.registry = registry
        self.access = access
        self.jobs: dict[tuple[RunContext, str], DeferredResult] = {}
        self.submissions: list[tuple[RunContext, str]] = []

    def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
        output = self.gateway.dispatch(call, version=version, context=context)
        approval = self.access.approvals.get((context, call.call_id))

        # The real gateway reaches consume_approval only after its other checks.
        # An undecided record becomes an approval wait in this test adapter.
        needs_approval = approval is not None and approval.granted is None

        if needs_approval and output.model_result == ToolFailure(
            call.call_id, ToolErrorCode.DENIED
        ):
            return EnforcedOutput(
                PendingToolCall(call.call_id, WaitReason.APPROVAL, f"approval-{call.call_id}")
            )

        if isinstance(output.model_result, RestrictedReceipt):
            reference = f"job-{call.call_id}"

            # Execution is immediate in this fake. Hold the already-enforced
            # outcome until a job notification, exercising WAITING and resumption.
            self.jobs[(context, reference)] = DeferredResult(call, version, output)
            self.submissions.append((context, reference))
            return EnforcedOutput(PendingToolCall(call.call_id, WaitReason.JOB, reference))

        return output

    def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
        if pending.reason is WaitReason.APPROVAL:
            approval = self.access.approvals[(context, pending.call_id)]
            return self.dispatch(approval.call, version=approval.version, context=context)

        job = self.jobs.pop((context, pending.reference_id))

        # Permissions may change while a result is held. Recheck them before the
        # completion receipt or delivery metadata leaves this fake provider.
        try:
            definition = self.registry.resolve(
                job.call.tool_name, job.version, self.access.permissions_for(context)
            )
        except ToolUnavailableError:
            return EnforcedOutput(ToolFailure(pending.call_id, ToolErrorCode.UNAVAILABLE))

        if not self.access.can_access(context, definition, job.call):
            return EnforcedOutput(ToolFailure(pending.call_id, ToolErrorCode.DENIED))

        return job.output

    def decide(
        self, context: RunContext, pending: PendingToolCall, *, granted: bool
    ) -> ContinuationEvent:
        self.access.approvals[(context, pending.call_id)].granted = granted
        return event_for(pending)


@dataclass(frozen=True)
class StoredReport:
    tenant_id: str
    user_id: str
    dataset: str
    body: str = field(repr=False)


class RuntimeHarness:
    """Shared runtime wiring for complete local workflows; all data is synthetic."""

    def __init__(self) -> None:
        self.model = ScriptedModel()
        self.access = FakeAccess()
        self.store = InMemoryStateStore()
        self.registry = ToolRegistry([CALCULATE, REPORT])
        self.executions: list[tuple[str, RunContext]] = []
        self.reports: dict[str, StoredReport] = {}
        self.contexts: list[RunContext] = []
        self.fail_calculation = False
        self.fail_filter = False
        self.fail_report = False
        self.gateway = ToolGateway(
            self.registry,
            {
                (CALCULATE.name, CALCULATE.version): ToolBinding(
                    self._calculate, validate_calculation, self._public_total
                ),
                (REPORT.name, REPORT.version): ToolBinding(self._report, validate_report),
            },
            self.access,
        )
        self.events = FakeEvents(self.gateway, self.registry, self.access)
        self.coordinator = Coordinator(
            self.store, self.registry, self.events, self.model, self.access.permissions_for
        )
        self.continuations = ContinuationHandler(self.store, self.coordinator, self.events)

    def start(
        self,
        context: RunContext = CONTEXT,
        message: str = "Calculate and prepare report",
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoordinatorResult:
        self.store.create_conversation(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )
        self.contexts.append(context)
        return self.coordinator.start(
            context,
            Message(MessageRole.USER, message),
            expected_conversation_revision=0,
            is_cancelled=is_cancelled,
        )

    def read_report(self, context: RunContext, artifact_id: str) -> str:
        report = self.reports[artifact_id]

        # This fake models an authenticated download boundary using verified
        # context. The real browser, backend and storage service are still absent.
        if (context.tenant_id, context.user_id) != (report.tenant_id, report.user_id):
            raise PermissionError("report unavailable")

        if "reports.read" not in self.access.permissions_for(context):
            raise PermissionError("report unavailable")

        if report.dataset not in self.access.datasets.get(
            (context.tenant_id, context.user_id), frozenset()
        ):
            raise PermissionError("report unavailable")

        return report.body

    def assert_public_boundaries(self) -> None:
        snapshots = self.model.request_payloads + [
            inspect_fields(self.store.load_run(context).conversation) for context in self.contexts
        ]

        for snapshot in snapshots:
            assert PRIVATE_BODY not in snapshot

            for artifact_id in self.reports:
                assert artifact_id not in snapshot

    def _calculate(
        self, context: RunContext, arguments: Mapping[str, JsonValue]
    ) -> ExecutionOutput:
        self.executions.append((CALCULATE.name, context))

        if self.fail_calculation:
            raise RuntimeError(PRIVATE_BODY)

        total = sum(cast(tuple[int, ...], arguments["values"]))
        return ExecutionOutput({"total": total, "private_notes": PRIVATE_BODY})

    def _public_total(self, payload: JsonValue) -> JsonValue:
        if self.fail_filter:
            raise ValueError(PRIVATE_BODY)

        return {"total": cast(Mapping[str, JsonValue], payload)["total"]}

    def _report(self, context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
        self.executions.append((REPORT.name, context))

        if self.fail_report:
            raise RuntimeError(PRIVATE_BODY)

        artifact_id = f"private-artifact-{len(self.reports) + 1}"
        body = f"{PRIVATE_BODY}: total={arguments['total']}"
        self.reports[artifact_id] = StoredReport(
            context.tenant_id, context.user_id, cast(str, arguments["dataset"]), body
        )
        return ExecutionOutput(
            {"body": body, "output_policy": "visible", "status": PRIVATE_BODY}, artifact_id
        )


def validate_calculation(arguments: Mapping[str, JsonValue]) -> None:
    values = arguments.get("values")

    if set(arguments) != {"values"} or not isinstance(values, tuple):
        raise ValueError(PRIVATE_BODY)

    if any(type(value) is not int for value in values):
        raise ValueError(PRIVATE_BODY)


def validate_report(arguments: Mapping[str, JsonValue]) -> None:
    if set(arguments) != {"dataset", "total"}:
        raise ValueError(PRIVATE_BODY)

    if type(arguments["dataset"]) is not str or type(arguments["total"]) is not int:
        raise ValueError(PRIVATE_BODY)


def event_for(pending: PendingToolCall) -> ContinuationEvent:
    return ContinuationEvent(pending.call_id, pending.reason, pending.reference_id)


def inspect_fields(value: object) -> str:
    """Capture nested dataclass fields and immutable mappings for leakage assertions."""

    return json.dumps(value, default=_json_fields)


def _json_fields(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}

    if isinstance(value, Mapping):
        return dict(value)

    raise TypeError(f"unsupported model input: {type(value).__name__}")
