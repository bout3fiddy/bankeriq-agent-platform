"""Gateway boundaries exercised with local handlers and an access-policy fake."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from agent_runtime.runs.context import RunContext
from agent_runtime.tools.bindings import ExecutionOutput, ToolBinding
from agent_runtime.tools.definitions import OutputPolicy, ToolCall, ToolDefinition
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry
from agent_runtime.tools.results import RestrictedReceipt, ToolErrorCode, ToolFailure, VisibleResult
from agent_runtime.types.json_value import JsonValue

CONTEXT = RunContext("tenant-1", "user-1", "conversation-1", "run-1")
CALL = ToolCall("call-1", "calculate", {"count": 2})
DEFINITION = ToolDefinition(
    "calculate",
    "1",
    "Synthetic calculation",
    {"type": "object"},
    OutputPolicy.VISIBLE,
    required_permissions=frozenset({"calculate"}),
)


@dataclass
class FakeAccess:
    permissions: frozenset[str] = frozenset({"calculate"})
    resource_allowed: bool = True
    approval: tuple[RunContext, str, ToolCall] | None = None
    events: list[str] = field(default_factory=list)

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        self.events.append("permissions")
        return self.permissions

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        self.events.append("resource")
        return self.resource_allowed

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:
        self.events.append("approval")

        if self.approval != (context, definition.version, call):
            return False

        self.approval = None
        return True


def make_gateway(
    access: FakeAccess, definition: ToolDefinition = DEFINITION
) -> tuple[ToolGateway, list[RunContext]]:
    executed: list[RunContext] = []

    def validate(arguments: Mapping[str, JsonValue]) -> None:
        access.events.append("validate")

        if set(arguments) != {"count"} or type(arguments["count"]) is not int:
            raise ValueError("PRIVATE_MARKER: unexpected arguments")

    def handler(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
        access.events.append("execute")
        executed.append(context)
        return ExecutionOutput({"total": arguments["count"], "private": "PRIVATE_MARKER"}, "file-1")

    def visible_filter(payload: JsonValue) -> JsonValue:
        return {"total": cast(Mapping[str, JsonValue], payload)["total"]}

    binding = ToolBinding(handler, validate, visible_filter)
    gateway = ToolGateway(
        ToolRegistry([definition]), {(definition.name, definition.version): binding}, access
    )
    return gateway, executed


def test_visible_execution_passes_trusted_identity_and_filters_output() -> None:
    access = FakeAccess()
    gateway, executed = make_gateway(access)
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert executed == [CONTEXT]
    assert access.events == ["permissions", "validate", "resource", "execute"]
    assert isinstance(result.model_result, VisibleResult)
    assert result.model_result.payload == {"total": 2}
    assert result.artifact is not None and result.artifact.artifact_id == "file-1"


def test_revoked_permission_blocks_a_later_call() -> None:
    access = FakeAccess()
    gateway, executed = make_gateway(access)
    gateway.dispatch(CALL, version="1", context=CONTEXT)
    access.permissions = frozenset()
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert len(executed) == 1
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.UNAVAILABLE)


@pytest.mark.parametrize("arguments", [{"count": "two"}, {"count": 2, "user_id": "forged"}])
def test_invalid_arguments_stop_before_resource_checks(arguments: dict[str, JsonValue]) -> None:
    access = FakeAccess()
    gateway, executed = make_gateway(access)
    result = gateway.dispatch(
        ToolCall("call-1", "calculate", arguments), version="1", context=CONTEXT
    )
    assert not executed
    assert access.events == ["permissions", "validate"]
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.INVALID_ARGUMENTS)


def test_resource_denial_prevents_approval_consumption_and_execution() -> None:
    access = FakeAccess(resource_allowed=False, approval=(CONTEXT, "1", CALL))
    gateway, executed = make_gateway(access, replace(DEFINITION, requires_approval=True))
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.DENIED)
    assert not executed
    assert access.approval is not None
    assert "approval" not in access.events


def test_approval_is_bound_to_arguments_and_consumed_once() -> None:
    access = FakeAccess(approval=(CONTEXT, "1", CALL))
    gateway, executed = make_gateway(access, replace(DEFINITION, requires_approval=True))
    changed = ToolCall("call-1", "calculate", {"count": 3})
    assert gateway.dispatch(changed, version="1", context=CONTEXT).model_result == ToolFailure(
        "call-1", ToolErrorCode.DENIED
    )
    assert not executed
    gateway.dispatch(CALL, version="1", context=CONTEXT)
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert executed == [CONTEXT]
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.DENIED)


def test_restricted_execution_returns_only_receipt_and_separate_artifact() -> None:
    gateway, _ = make_gateway(
        FakeAccess(), replace(DEFINITION, output_policy=OutputPolicy.RESTRICTED)
    )
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert result.model_result == RestrictedReceipt("call-1")
    assert result.artifact is not None
    assert "PRIVATE_MARKER" not in repr(result)


@pytest.mark.parametrize("name,version", [("unknown", "1"), ("calculate", "2")])
def test_unknown_name_or_version_never_executes(name: str, version: str) -> None:
    gateway, executed = make_gateway(FakeAccess())
    result = gateway.dispatch(
        ToolCall("call-1", name, {"count": 2}), version=version, context=CONTEXT
    )
    assert not executed
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.UNAVAILABLE)


def test_missing_filter_blocks_side_effects() -> None:
    def forbidden_handler(
        context: RunContext, arguments: Mapping[str, JsonValue]
    ) -> ExecutionOutput:
        pytest.fail("handler executed without configured output filter")

    binding = ToolBinding(forbidden_handler, lambda arguments: None)
    gateway = ToolGateway(ToolRegistry([DEFINITION]), {("calculate", "1"): binding}, FakeAccess())
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.EXECUTION_FAILED)


def test_handler_exception_is_sanitised() -> None:
    def failing_handler(context: RunContext, arguments: Mapping[str, JsonValue]) -> ExecutionOutput:
        raise RuntimeError("PRIVATE_MARKER")

    binding = ToolBinding(failing_handler, lambda arguments: None, lambda payload: payload)
    gateway = ToolGateway(ToolRegistry([DEFINITION]), {("calculate", "1"): binding}, FakeAccess())
    result = gateway.dispatch(CALL, version="1", context=CONTEXT)
    assert result.model_result == ToolFailure("call-1", ToolErrorCode.EXECUTION_FAILED)
    assert "PRIVATE_MARKER" not in repr(result)
