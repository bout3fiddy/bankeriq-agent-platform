"""Trusted run identity and lifecycle snapshots."""

from dataclasses import dataclass

from ..tools.results import PendingToolCall
from .enums import RunStatus


# Dataclasses provide explicit fields, equality and constructors for these values.
# frozen prevents ordinary reassignment; slots prevents extra instance fields.
# JSON snapshot helpers freeze nested values. Security checks belong to the gateway.
# Keep identity separate from tool arguments so model output cannot supply it.
@dataclass(frozen=True, slots=True)
class RunContext:
    """Identity bound by the trusted caller; permissions are checked separately."""

    tenant_id: str
    user_id: str
    conversation_id: str
    run_id: str


# A snapshot records execution progress. Multiple pending calls allow one model
# step to request several tools.
@dataclass(frozen=True, slots=True)
class RunState:
    """Lifecycle snapshot; storage and transition enforcement are separate work."""

    context: RunContext
    status: RunStatus
    pending_calls: tuple[PendingToolCall, ...] = ()

    def __post_init__(self) -> None:

        if (self.status is RunStatus.WAITING) != bool(self.pending_calls):
            raise ValueError("only waiting runs must have pending calls")

        # Duplicate IDs would make a completion event ambiguous within this run.
        if len({call.call_id for call in self.pending_calls}) != len(self.pending_calls):
            raise ValueError("pending call IDs must be unique within a run")
