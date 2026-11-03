"""Versioned recovery envelope and trusted compatible handler registrations."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import ArtifactReference, PendingToolCall
from bankeriq_agent_platform.storage.codec import strict_json


class Recovery(BaseModel):
    """Only permitted, validated arguments or opaque references belong in this envelope.

    For example, Recovery(kind="sum", operation_id="sum-7", arguments={"a": 2})
    survives a process restart. The registration supplies fresh clients and current
    access checks; neither credentials nor a serialized callable belongs here.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format_version: Literal[1] = 1
    kind: str = Field(min_length=1, max_length=100)
    version: int = Field(default=1, ge=1)
    phase: Literal["ready", "dispatch", "retry", "approval", "reconcile", "complete"] = "ready"
    operation_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = False
    replay_allowed: bool = False
    attempt_limit: int = Field(default=3, ge=1, le=100)
    run_budget: int = Field(default=16, ge=1, le=1000)

    def encode(self) -> dict[str, Any]:

        # Validate before any write. NaN, integer object keys, bytes and handles
        # must fail here, even when a nested value happens to be JSON-coercible.
        value = self.model_dump()
        type(self).model_validate(value)
        strict_json(value, maximum=60000)
        return value

    @classmethod
    def decode(cls, value: Any) -> Recovery:
        strict_json(value, maximum=60000)
        return cls.model_validate(value)


@dataclass(frozen=True)
class ChildTask:
    """A pinned call and trusted execution policy; resource keys never come from the model.

    Two calculate calls with identical arguments retain different call IDs. A trusted
    binder can assign resource_key="account-17" to serialize conflicting operations
    across runs in the same tenant. Omitting the key permits bounded parallelism.
    """

    call: ToolCall
    recovery: Recovery
    resource_key: str | None = None


@dataclass(frozen=True)
class Completion:
    """An enforced delta and optional next synthetic task, committed together.

    A handler returns Completion(RunStatus.RUNNING, next_task=Recovery(...)) to
    advance revision 0 to 1 and install the next handler on the generated job.
    Restricted operations return RestrictedReceipt plus opaque ArtifactReference;
    their trusted handler must apply the existing output policy before returning.
    """

    status: RunStatus
    items: tuple[ConversationItem, ...] = ()
    pending: tuple[PendingToolCall, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    next_task: Recovery | None = None
    children: tuple[ChildTask, ...] = ()
    failure_policy: Literal["wait_all", "fail_fast"] = "wait_all"
    prepare_model: bool = False
    model_steps: int | None = None
    tool_calls: int | None = None


@dataclass(frozen=True)
class Handler:
    """Fresh process dependencies and explicit replay/compatibility policy.

    Example: Handler(lambda context, state: True, synthetic_sum, idempotent=True).
    synthetic_sum(context, state) uses state.operation_id for provider deduplication
    and returns a Completion. authorize runs again after every wait and recovery.
    reconcile looks up that same operation ID and returns a proven result or None;
    None leaves the job in reconciliation without executing it again.
    approval_valid(context, state) checks the current approval/revocation source;
    for example, lambda context, state: approvals.active(state.operation_id).
    Omitting it declares that no additional provider approval is required; the
    queue still checks any persisted approval deadline before dispatch and lookup.
    """

    authorize: Callable[[RunContext, Recovery], bool]
    execute: Callable[[RunContext, Recovery], Completion]
    idempotent: bool = False
    replay_allowed: bool = False
    reconcile: Callable[[RunContext, Recovery], Completion | None] | None = None
    async_execute: Callable[[RunContext, Recovery], Awaitable[Completion]] | None = None
    approval_required: Callable[[RunContext, Recovery], bool] = lambda context, state: False
    approval_valid: Callable[[RunContext, Recovery], bool] = lambda context, state: True


class Handlers:
    def __init__(self, entries: Mapping[tuple[str, int], Handler]) -> None:
        self.entries = MappingProxyType(dict(entries))

    def resolve(self, state: Recovery) -> Handler:
        try:
            return self.entries[state.kind, state.version]
        except KeyError:
            raise ValueError("missing compatible recovery handler") from None


def replay_safe(state: Recovery, handler: Handler) -> bool:
    return (state.idempotent and handler.idempotent) or (
        state.replay_allowed and handler.replay_allowed
    )


def permitted(check: Callable[[], bool]) -> bool:
    """Policy/client exceptions produce a fixed denial without exposing their text."""

    try:
        return check()
    except Exception:
        return False


def policy_permitted(
    policy: Callable[[RunContext, Recovery], bool],
    context: RunContext,
    state: Recovery,
) -> bool:
    """Give injected policy code a detached snapshot of the exact saved intent."""

    # Recovery is frozen, but nested JSON dictionaries can still be mutated.
    # A policy that changes {"amount": 2} to {"amount": 3} must neither alter
    # the saved arguments nor authorize a different operation accidentally.
    # Compare canonical JSON after the callback and collapse failures to denial.
    encoded = strict_json(state.encode())
    candidate = Recovery.model_validate_json(encoded)

    try:
        return policy(context, candidate) and strict_json(candidate.encode()) == encoded
    except Exception:
        return False
