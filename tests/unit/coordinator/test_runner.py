"""Coordinator behaviour with scripted model replies and local execution fakes."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.control import RunLimits
from agent_runtime.coordinator.model import ModelReply, ModelRequest, ReasoningEffort
from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.policy.output import EnforcedOutput
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError
from agent_runtime.storage.memory import InMemoryStateStore
from agent_runtime.tools.bindings import ExecutionOutput, ToolBinding
from agent_runtime.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry
from agent_runtime.tools.results import (
    PendingToolCall,
    RestrictedReceipt,
    ToolErrorCode,
    ToolFailure,
    VisibleResult,
    WaitReason,
)
from agent_runtime.types.json_value import JsonValue

CONTEXT = RunContext("tenant", "user", "chat", "run")
CALL = ToolCall("call-1", "calculate", {"count": 2})


@dataclass
class ScriptedModel:
    replies: list[ModelReply | Exception]
    requests: list[ModelRequest] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        reply = self.replies.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return reply


@dataclass
class FakeAccess:
    permissions: frozenset[str] = frozenset({"calculate"})

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        return self.permissions

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        return True

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:
        return False


def setup(
    model: ScriptedModel,
    *,
    policy: OutputPolicy = OutputPolicy.VISIBLE,
    limits: RunLimits | None = None,
    reasoning_effort: ReasoningEffort | None = None,
) -> tuple[Coordinator, InMemoryStateStore, FakeAccess, list[RunContext]]:
    store = InMemoryStateStore()
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    definition = ToolDefinition(
        "calculate",
        "version-2",
        "Synthetic total",
        {"type": "object"},
        policy,
        required_permissions=frozenset({"calculate"}),
    )
    registry = ToolRegistry([definition])
    access = FakeAccess()
    executed: list[RunContext] = []

    def handler(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
        executed.append(context)
        return ExecutionOutput({"total": 2, "private": "PRIVATE_MARKER"}, "artifact-1")

    def public_total(payload: JsonValue) -> JsonValue:
        return {"total": cast(Mapping[str, JsonValue], payload)["total"]}

    gateway = ToolGateway(
        registry,
        {("calculate", "version-2"): ToolBinding(handler, lambda arguments: None, public_total)},
        access,
    )
    return (
        Coordinator(
            store,
            registry,
            gateway,
            model,
            access.permissions_for,
            limits,
            reasoning_effort=reasoning_effort,
            prompts=FixedPromptResolver("Synthetic test instructions"),
        ),
        store,
        access,
        executed,
    )


def test_visible_tool_cycle_checkpoints_and_returns_final_answer() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Total is 2")])
    coordinator, store, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Calculate"), expected_conversation_revision=0
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.answer == "Total is 2"
    assert executed == [CONTEXT]
    assert result.artifacts[0].artifact_id == "artifact-1"
    history = model.requests[1].history
    assert history[1] == CALL
    assert isinstance(history[2], VisibleResult)
    assert history[2].payload == {"total": 2}
    assert model.requests[0].tools[0].name == "calculate"
    assert store.load_run(CONTEXT).conversation.items[-1] == Message(
        MessageRole.ASSISTANT, "Total is 2"
    )


def test_multiple_tool_batches_feed_results_back_to_model() -> None:
    second_call = ToolCall("call-2", "calculate", {"count": 3})
    followup_call = ToolCall("call-3", "calculate", {"count": 4})
    model = ScriptedModel(
        [
            ModelReply(calls=(CALL, second_call)),
            ModelReply(calls=(followup_call,)),
            ModelReply(answer="Three calculations finished"),
        ]
    )
    coordinator, _, _, executed = setup(model, limits=RunLimits(model_steps=3, tool_calls=3))
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Calculate"), expected_conversation_revision=0
    )

    # Each model request sees the preceding batch's calls and permitted results.
    assert model.requests[1].history[1:] == (
        CALL,
        second_call,
        VisibleResult("call-1", {"total": 2}),
        VisibleResult("call-2", {"total": 2}),
    )
    assert model.requests[2].history == model.requests[1].history + (
        followup_call,
        VisibleResult("call-3", {"total": 2}),
    )
    assert executed == [CONTEXT, CONTEXT, CONTEXT]
    assert [artifact.call_id for artifact in result.artifacts] == ["call-1", "call-2", "call-3"]
    assert result.run.status is RunStatus.COMPLETED
    assert result.answer == "Three calculations finished"


def test_restricted_payload_and_artifact_never_enter_model_history() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Report ready")])
    coordinator, store, _, executed = setup(model, policy=OutputPolicy.RESTRICTED)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Report"), expected_conversation_revision=0
    )

    assert executed == [CONTEXT]
    assert model.requests[1].history[-1] == RestrictedReceipt("call-1")
    assert result.artifacts[0].artifact_id == "artifact-1"
    assert store.load_run(CONTEXT).conversation.items == (
        Message(MessageRole.USER, "Report"),
        CALL,
        RestrictedReceipt("call-1"),
        Message(MessageRole.ASSISTANT, "Report ready"),
    )


def test_unknown_tool_returns_error_without_execution() -> None:
    unknown = ToolCall("call-x", "unknown", {})
    model = ScriptedModel([ModelReply(calls=(unknown,)), ModelReply(answer="Unavailable")])
    coordinator, _, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert not executed
    assert model.requests[1].history[-1] == ToolFailure("call-x", ToolErrorCode.UNAVAILABLE)
    assert result.run.status is RunStatus.COMPLETED


def test_current_permissions_filter_discovery_and_dispatch() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Unavailable")])
    coordinator, _, access, executed = setup(model)
    access.permissions = frozenset()
    coordinator.start(CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0)
    assert all(request.tools == () for request in model.requests)
    assert not executed


@pytest.mark.parametrize(
    "reply",
    [ModelReply(), ModelReply(answer="Ambiguous", calls=(CALL,)), ModelReply(calls=(CALL, CALL))],
)
def test_malformed_reply_fails_before_tool_execution(reply: ModelReply) -> None:
    model = ScriptedModel([reply])
    coordinator, store, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.FAILED
    assert not executed
    assert len(store.load_run(CONTEXT).conversation.items) == 1


def test_repeated_call_id_is_rejected_before_reexecution() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(calls=(CALL,))])
    coordinator, _, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.FAILED
    assert len(executed) == 1


def test_model_step_limit_stops_tool_loop() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    coordinator, _, _, executed = setup(model, limits=RunLimits(model_steps=1))
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.LIMIT_REACHED
    assert len(executed) == 1
    assert len(model.requests) == 1


def test_tool_budget_rejects_whole_batch_before_side_effects() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL, ToolCall("call-2", "calculate", {})))])
    coordinator, _, _, executed = setup(model, limits=RunLimits(tool_calls=1))
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.LIMIT_REACHED
    assert not executed


def test_cancellation_before_model_call() -> None:
    model = ScriptedModel([])
    coordinator, _, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT,
        Message(MessageRole.USER, "Try"),
        expected_conversation_revision=0,
        is_cancelled=lambda: True,
    )
    assert result.run.status is RunStatus.CANCELLED
    assert model.requests == []
    assert not executed


def test_cancellation_after_model_prevents_tool_execution() -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,))])
    coordinator, _, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT,
        Message(MessageRole.USER, "Try"),
        expected_conversation_revision=0,
        is_cancelled=lambda: bool(model.requests),
    )
    assert result.run.status is RunStatus.CANCELLED
    assert not executed


def test_cancellation_between_tools_saves_completed_results() -> None:
    second_call = ToolCall("call-2", "calculate", {"count": 3})
    model = ScriptedModel([ModelReply(calls=(CALL, second_call))])
    coordinator, store, _, executed = setup(model)
    result = coordinator.start(
        CONTEXT,
        Message(MessageRole.USER, "Calculate"),
        expected_conversation_revision=0,
        is_cancelled=lambda: bool(executed),
    )

    assert result.run.status is RunStatus.CANCELLED
    assert executed == [CONTEXT]
    assert len(model.requests) == 1
    assert [artifact.call_id for artifact in result.artifacts] == ["call-1"]
    assert store.load_run(CONTEXT).conversation.items == (
        Message(MessageRole.USER, "Calculate"),
        CALL,
        second_call,
        VisibleResult("call-1", {"total": 2}),
    )


def test_model_failure_is_sanitised_and_run_cannot_be_replayed() -> None:
    model = ScriptedModel([RuntimeError("PRIVATE_MARKER")])
    coordinator, store, _, _ = setup(model)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.FAILED
    assert result.answer is None
    assert len(store.load_run(CONTEXT).conversation.items) == 1

    with pytest.raises(StateConflictError):
        coordinator.run(CONTEXT)

    assert len(model.requests) == 1


def test_pending_gateway_result_suspends_without_polling() -> None:
    class PendingGateway:
        def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
            return EnforcedOutput(PendingToolCall(call.call_id, WaitReason.JOB, "job-1"))

    model = ScriptedModel([ModelReply(calls=(CALL,))])
    _, store, access, _ = setup(model)
    definition = ToolDefinition(
        "calculate", "1", "Synthetic", {"type": "object"}, OutputPolicy.RESTRICTED
    )
    coordinator = Coordinator(
        store,
        ToolRegistry([definition]),
        PendingGateway(),
        model,
        access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Try"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.WAITING
    assert result.run.pending_calls == (PendingToolCall("call-1", WaitReason.JOB, "job-1"),)
    assert len(model.requests) == 1

    with pytest.raises(StateConflictError):
        coordinator.run(CONTEXT)


@pytest.mark.parametrize("effort", [None, *ReasoningEffort])
def test_reasoning_effort_is_forwarded_on_every_model_step(effort: ReasoningEffort | None) -> None:
    model = ScriptedModel([ModelReply(calls=(CALL,)), ModelReply(answer="Total is 2")])
    coordinator, _, _, _ = setup(model, reasoning_effort=effort)
    result = coordinator.start(
        CONTEXT, Message(MessageRole.USER, "Calculate"), expected_conversation_revision=0
    )
    assert result.run.status is RunStatus.COMPLETED
    assert [request.reasoning_effort for request in model.requests] == [effort, effort]


def test_explicit_no_reasoning_is_distinct_from_provider_default() -> None:
    default_request = ModelRequest((), ())
    disabled_request = ModelRequest((), (), reasoning_effort=ReasoningEffort.NONE)
    assert default_request.reasoning_effort is None
    assert disabled_request.reasoning_effort is ReasoningEffort.NONE
