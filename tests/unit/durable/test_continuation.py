"""Continuation matching, policy boundaries and resumption with local fakes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from threading import Event

import pytest

from agent_harness.continuations.continuation import ContinuationEvent
from agent_harness.continuations.handler import ContinuationHandler
from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.control import RunLimits
from agent_harness.coordinator.model import ModelReply, ModelRequest
from agent_harness.coordinator.runner import Coordinator
from agent_harness.policy.output import EnforcedOutput, enforce_output
from agent_harness.prompts.resolution import FixedPromptResolver
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunStatus
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_harness.tools.bindings import ExecutionOutput, ToolBinding
from agent_harness.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_harness.tools.gateway import ToolGateway
from agent_harness.tools.registry import ToolRegistry
from agent_harness.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
    WaitReason,
)
from agent_runtime.storage.memory import InMemoryStateStore

CONTEXT = RunContext("tenant", "user", "chat", "run")
CALL = ToolCall("call-1", "report", {})
DEFINITION = ToolDefinition(
    "report", "v2", "Synthetic report", {"type": "object"}, OutputPolicy.RESTRICTED
)
JOB_EVENT = ContinuationEvent(CALL.call_id, WaitReason.JOB, "job-call-1")


@dataclass
class ScriptedModel:
    replies: list[ModelReply]
    requests: list[ModelRequest] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        return self.replies.pop(0)


@dataclass
class PendingTools:
    outcomes: dict[str, EnforcedOutput | Exception] = field(default_factory=dict)
    reason: WaitReason = WaitReason.JOB
    dispatched: list[tuple[RunContext, ToolCall, str]] = field(default_factory=list)
    resolved: list[PendingToolCall] = field(default_factory=list)

    def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
        self.dispatched.append((context, call, version))
        return EnforcedOutput(
            PendingToolCall(call.call_id, self.reason, f"{self.reason}-{call.call_id}")
        )

    def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
        self.resolved.append(pending)
        output = self.outcomes[pending.reference_id]

        if isinstance(output, Exception):
            raise output

        return output


def coordinator_for(
    store: InMemoryStateStore,
    model: ScriptedModel,
    tools: PendingTools,
    limits: RunLimits | None = None,
    definition: ToolDefinition = DEFINITION,
) -> Coordinator:
    return Coordinator(
        store,
        ToolRegistry([definition]),
        tools,
        model,
        lambda context: frozenset(),
        limits,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )


def setup(
    model: ScriptedModel,
    tools: PendingTools,
    limits: RunLimits | None = None,
    definition: ToolDefinition = DEFINITION,
) -> tuple[ContinuationHandler, InMemoryStateStore]:
    store = InMemoryStateStore()
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    coordinator = coordinator_for(store, model, tools, limits, definition)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Prepare report"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.WAITING
    return ContinuationHandler(store, coordinator, tools), store


def test_restricted_job_resumes_from_stored_state_and_delivers_artifact_separately() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Report ready")])
    output = enforce_output(
        DEFINITION, CALL.call_id, {"private": "PRIVATE_MARKER"}, artifact_id="artifact-1"
    )
    tools = PendingTools({JOB_EVENT.reference_id: output})
    _, store = setup(model, tools)

    # A new coordinator object uses the same stored state and trusted providers.
    coordinator = coordinator_for(store, model, tools)
    continuations = ContinuationHandler(store, coordinator, tools)
    result = continuations.resume(CONTEXT, JOB_EVENT)
    assert result.run.status is RunStatus.COMPLETED
    assert result.answer == "Report ready"
    assert result.run.model_steps == 2
    assert result.run.tool_calls == 1
    assert result.artifacts == (ArtifactReference(CALL.call_id, "artifact-1"),)
    assert model.requests[1].history[-1] == RestrictedReceipt(CALL.call_id)
    assert len(tools.dispatched) == 1

    with pytest.raises(StateConflictError):
        continuations.resume(CONTEXT, JOB_EVENT)

    assert len(tools.resolved) == 1
    assert len(model.requests) == 2


def test_visible_job_output_is_filtered_before_next_model_call() -> None:
    definition = replace(DEFINITION, output_policy=OutputPolicy.VISIBLE)
    output = enforce_output(
        definition,
        CALL.call_id,
        {"total": 42, "private": "PRIVATE_MARKER"},
        visible_filter=lambda payload: {"total": 42},
    )
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Total is 42")])
    continuations, _ = setup(
        model, PendingTools({JOB_EVENT.reference_id: output}), definition=definition
    )
    result = continuations.resume(CONTEXT, JOB_EVENT)
    assert result.run.status is RunStatus.COMPLETED
    assert model.requests[1].history[-1] == VisibleResult(CALL.call_id, {"total": 42})


def test_out_of_order_jobs_wait_for_whole_batch_and_reject_partial_duplicate() -> None:
    second_call = ToolCall("call-2", "report", {})
    second_event = ContinuationEvent("call-2", WaitReason.JOB, "job-call-2")
    model = ScriptedModel([ModelReply(calls=(CALL, second_call)), ModelReply(answer="Both ready")])
    tools = PendingTools(
        {
            JOB_EVENT.reference_id: EnforcedOutput(RestrictedReceipt(CALL.call_id)),
            second_event.reference_id: EnforcedOutput(
                RestrictedReceipt("call-2"), ArtifactReference("call-2", "artifact-2")
            ),
        }
    )
    continuations, store = setup(model, tools)
    partial = continuations.resume(CONTEXT, second_event)
    assert partial.run.status is RunStatus.WAITING
    assert partial.run.pending_calls == (
        PendingToolCall(CALL.call_id, WaitReason.JOB, "job-call-1"),
    )
    assert partial.artifacts == (ArtifactReference("call-2", "artifact-2"),)
    assert len(model.requests) == 1
    saved = store.load_run(CONTEXT)

    with pytest.raises(StateConflictError):
        continuations.resume(CONTEXT, second_event)

    assert store.load_run(CONTEXT) == saved
    final = continuations.resume(CONTEXT, JOB_EVENT)
    assert final.run.status is RunStatus.COMPLETED
    assert final.artifacts == ()
    assert model.requests[1].history[-2:] == (
        RestrictedReceipt("call-2"),
        RestrictedReceipt(CALL.call_id),
    )


def test_resumed_model_can_request_another_job_before_answering() -> None:
    next_call = ToolCall("call-2", "report", {})
    model = ScriptedModel(
        [ModelReply(calls=(CALL,)), ModelReply(calls=(next_call,)), ModelReply(answer="Ready")]
    )
    tools = PendingTools(
        {
            "job-call-1": EnforcedOutput(RestrictedReceipt("call-1")),
            "job-call-2": EnforcedOutput(RestrictedReceipt("call-2")),
        }
    )
    continuations, _ = setup(model, tools)
    resumed = continuations.resume(CONTEXT, JOB_EVENT)
    assert resumed.run.status is RunStatus.WAITING
    assert resumed.run.pending_calls == (PendingToolCall("call-2", WaitReason.JOB, "job-call-2"),)
    final = continuations.resume(CONTEXT, ContinuationEvent("call-2", WaitReason.JOB, "job-call-2"))
    assert final.run.status is RunStatus.COMPLETED
    assert final.run.model_steps == 3
    assert final.run.tool_calls == 2
    assert model.requests[2].history[-1] == RestrictedReceipt("call-2")


@pytest.mark.parametrize(
    "event",
    [
        replace(JOB_EVENT, call_id="other-call"),
        replace(JOB_EVENT, reference_id="other-job"),
        replace(JOB_EVENT, reason=WaitReason.APPROVAL),
    ],
)
def test_unmatched_event_preserves_state_without_resolving(event: ContinuationEvent) -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    tools = PendingTools()
    continuations, store = setup(model, tools)
    saved = store.load_run(CONTEXT)

    with pytest.raises(StateConflictError):
        continuations.resume(CONTEXT, event)

    assert store.load_run(CONTEXT) == saved
    assert tools.resolved == []
    assert len(model.requests) == 1


@pytest.mark.parametrize("field_name", ["tenant_id", "user_id", "conversation_id", "run_id"])
def test_continuation_checks_full_context_ownership(field_name: str) -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    tools = PendingTools()
    continuations, store = setup(model, tools)
    saved = store.load_run(CONTEXT)

    with pytest.raises(StateNotFoundError):
        continuations.resume(replace(CONTEXT, **{field_name: "other"}), JOB_EVENT)

    assert store.load_run(CONTEXT) == saved
    assert tools.resolved == []


def test_approval_can_handoff_to_job_without_another_model_call() -> None:
    approval_event = ContinuationEvent(CALL.call_id, WaitReason.APPROVAL, "approval-call-1")
    job = PendingToolCall(CALL.call_id, WaitReason.JOB, "job-call-1")
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Ready")])
    tools = PendingTools(
        {
            approval_event.reference_id: EnforcedOutput(job),
            job.reference_id: EnforcedOutput(RestrictedReceipt(CALL.call_id)),
        },
        reason=WaitReason.APPROVAL,
    )
    continuations, _ = setup(model, tools)
    waiting = continuations.resume(CONTEXT, approval_event)
    assert waiting.run.status is RunStatus.WAITING
    assert waiting.run.pending_calls == (job,)
    assert len(model.requests) == 1

    with pytest.raises(StateConflictError):
        continuations.resume(CONTEXT, approval_event)

    final = continuations.resume(CONTEXT, JOB_EVENT)
    assert final.run.status is RunStatus.COMPLETED
    assert final.run.tool_calls == 1


@pytest.mark.parametrize(
    "approved,permission_retained,failure",
    [
        (True, True, None),
        (False, True, ToolErrorCode.DENIED),
        (True, False, ToolErrorCode.UNAVAILABLE),
    ],
)
def test_approval_resolution_uses_gateway_and_current_access(
    approved: bool, permission_retained: bool, failure: ToolErrorCode | None
) -> None:
    definition = replace(
        DEFINITION, requires_approval=True, required_permissions=frozenset({"report"})
    )
    registry = ToolRegistry([definition])
    approval_checks: list[tuple[RunContext, ToolDefinition, ToolCall]] = []
    executed: list[RunContext] = []

    class FakeAccess:
        permissions = frozenset({"report"})
        approval_present = False

        def permissions_for(self, context: RunContext) -> frozenset[str]:
            return self.permissions

        def can_access(
            self, context: RunContext, definition: ToolDefinition, call: ToolCall
        ) -> bool:
            return True

        def consume_approval(
            self, context: RunContext, definition: ToolDefinition, call: ToolCall
        ) -> bool:
            approval_checks.append((context, definition, call))
            was_approved = self.approval_present
            self.approval_present = False
            return was_approved

    access = FakeAccess()

    def handler(context: RunContext, arguments: object) -> ExecutionOutput:
        executed.append(context)
        return ExecutionOutput({"private": "PRIVATE_MARKER"}, "artifact-1")

    gateway = ToolGateway(
        registry,
        {(definition.name, definition.version): ToolBinding(handler, lambda arguments: None)},
        access,
    )

    class ApprovalTools(PendingTools):
        def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
            self.resolved.append(pending)

            # The fake pending provider retained the call and version at dispatch.
            # Production approval storage must also bind context and arguments.
            original_context, original_call, version = self.dispatched[0]
            assert context == original_context
            return gateway.dispatch(original_call, version=version, context=context)

    tools = ApprovalTools(reason=WaitReason.APPROVAL)
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Request resolved")])
    store = InMemoryStateStore()
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    coordinator = Coordinator(
        store,
        registry,
        tools,
        model,
        access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )
    continuations = ContinuationHandler(store, coordinator, tools)
    waiting = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Prepare report"), expected_conversation_revision=0
    )
    assert waiting.run.status is RunStatus.WAITING

    # A notification is identical in all cases. The current provider state
    # determines whether execution is allowed when the wait is resolved.
    access.approval_present = approved
    access.permissions = frozenset({"report"}) if permission_retained else frozenset()
    event = ContinuationEvent(CALL.call_id, WaitReason.APPROVAL, "approval-call-1")
    result = continuations.resume(CONTEXT, event)
    assert result.run.status is RunStatus.COMPLETED

    if failure is None:
        assert executed == [CONTEXT]
        assert model.requests[1].history[-1] == RestrictedReceipt(CALL.call_id)
        assert result.artifacts == (ArtifactReference(CALL.call_id, "artifact-1"),)
    else:
        assert executed == []
        assert result.artifacts == ()
        assert model.requests[1].history[-1] == ToolFailure(CALL.call_id, failure)

    if permission_retained:
        assert approval_checks == [(CONTEXT, definition, CALL)]
        assert not access.approval_present
    else:
        assert approval_checks == []
        assert model.requests[1].tools == ()


@pytest.mark.parametrize(
    "outcome",
    [
        RuntimeError("PRIVATE_MARKER"),
        EnforcedOutput(VisibleResult("wrong-call", "PRIVATE_MARKER")),
        EnforcedOutput(RestrictedReceipt(CALL.call_id), ArtifactReference("wrong-call", "private")),
        EnforcedOutput(PendingToolCall(CALL.call_id, WaitReason.JOB, "job-call-1")),
    ],
)
def test_invalid_resolution_becomes_fixed_failure(outcome: EnforcedOutput | Exception) -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Job failed")])
    continuations, _ = setup(model, PendingTools({JOB_EVENT.reference_id: outcome}))
    result = continuations.resume(CONTEXT, JOB_EVENT)
    assert result.run.status is RunStatus.COMPLETED
    assert result.artifacts == ()
    assert model.requests[1].history[-1] == ToolFailure(
        CALL.call_id, ToolErrorCode.EXECUTION_FAILED
    )


@pytest.mark.parametrize("cancel_before_resolving", [True, False])
def test_cancellation_around_resolution_stops_model_calls(cancel_before_resolving: bool) -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    output = EnforcedOutput(
        RestrictedReceipt(CALL.call_id), ArtifactReference(CALL.call_id, "artifact-1")
    )
    tools = PendingTools({JOB_EVENT.reference_id: output})
    continuations, store = setup(model, tools)
    result = continuations.resume(
        CONTEXT,
        JOB_EVENT,
        is_cancelled=lambda: cancel_before_resolving or bool(tools.resolved),
    )
    assert result.run.status is RunStatus.CANCELLED
    assert result.run.pending_calls == ()
    assert len(model.requests) == 1

    if cancel_before_resolving:
        assert tools.resolved == []
        assert result.artifacts == ()
    else:
        assert len(tools.resolved) == 1
        assert result.artifacts == (ArtifactReference(CALL.call_id, "artifact-1"),)
        assert store.load_run(CONTEXT).conversation.items[-1] == RestrictedReceipt(CALL.call_id)


def test_model_budget_survives_wait_and_new_coordinator_instance() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    tools = PendingTools({JOB_EVENT.reference_id: EnforcedOutput(RestrictedReceipt(CALL.call_id))})
    limits = RunLimits(model_steps=1)
    _, store = setup(model, tools, limits)
    coordinator = coordinator_for(store, model, tools, limits)
    continuations = ContinuationHandler(store, coordinator, tools)
    result = continuations.resume(CONTEXT, JOB_EVENT)
    assert result.run.status is RunStatus.LIMIT_REACHED
    assert result.run.model_steps == 1
    assert len(model.requests) == 1


def test_tool_budget_survives_wait() -> None:
    model = ScriptedModel(
        [ModelReply(calls=(CALL,)), ModelReply(calls=(ToolCall("call-2", "report", {}),))]
    )
    tools = PendingTools({JOB_EVENT.reference_id: EnforcedOutput(RestrictedReceipt(CALL.call_id))})
    continuations, _ = setup(model, tools, RunLimits(tool_calls=1))
    result = continuations.resume(CONTEXT, JOB_EVENT)
    assert result.run.status is RunStatus.LIMIT_REACHED
    assert result.run.tool_calls == 1
    assert len(tools.dispatched) == 1


def test_duplicate_consumer_cannot_resolve_claimed_wait() -> None:
    entered = Event()
    release = Event()

    class BlockingTools(PendingTools):
        def resolve(self, context: RunContext, pending: PendingToolCall) -> EnforcedOutput:
            entered.set()

            if not release.wait(timeout=5):
                raise TimeoutError("test did not release resolver")

            return super().resolve(context, pending)

    tools = BlockingTools({JOB_EVENT.reference_id: EnforcedOutput(RestrictedReceipt(CALL.call_id))})
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Ready")])
    continuations, _ = setup(model, tools)

    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(continuations.resume, CONTEXT, JOB_EVENT)

        try:
            assert entered.wait(timeout=5)

            with pytest.raises(StateConflictError):
                continuations.resume(CONTEXT, JOB_EVENT)
        finally:
            release.set()

        assert first.result(timeout=5).run.status is RunStatus.COMPLETED

    assert len(tools.resolved) == 1
    assert len(model.requests) == 2
