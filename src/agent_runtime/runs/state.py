"""Run lifecycle snapshots and transition rules."""

from dataclasses import dataclass
from enum import StrEnum

from ..tools.results import PendingToolCall
from .context import RunContext


# Keep waiting, cancellation and budget exhaustion distinct from failure so the
# coordinator can explain why work stopped and whether continuation is possible.
class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"


# A snapshot records execution progress. Multiple pending calls allow one model
# step to request several tools.
@dataclass(frozen=True, slots=True)
class RunState:
    """Stored progress and budgets for a run, including its waits and resumptions."""

    context: RunContext
    status: RunStatus
    pending_calls: tuple[PendingToolCall, ...] = ()
    revision: int = 0
    model_steps: int = 0
    tool_calls: int = 0

    def __post_init__(self) -> None:

        if self.status is RunStatus.WAITING and not self.pending_calls:
            raise ValueError("waiting runs must have pending calls")

        # A continuation claims WAITING -> RUNNING before resolving an approval
        # or job. Keep its pending references until the outcome has been saved.
        if self.pending_calls and self.status not in (RunStatus.WAITING, RunStatus.RUNNING):
            raise ValueError("pending calls require a waiting or running run")

        # Duplicate IDs would make a completion event ambiguous within this run.
        if len({call.call_id for call in self.pending_calls}) != len(self.pending_calls):
            raise ValueError("pending call IDs must be unique within a run")


TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.LIMIT_REACHED}
)

# Same-status updates save progress. WAITING can hold multiple outstanding jobs;
# completing one can leave the run waiting for the others.
_TRANSITIONS = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.RUNNING, RunStatus.WAITING}) | TERMINAL_STATUSES,
    RunStatus.WAITING: frozenset(
        {RunStatus.WAITING, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
}


def validate_transition(current: RunStatus, requested: RunStatus) -> None:
    """Reject lifecycle changes that would skip execution or reopen a finished run."""

    # For example, QUEUED -> RUNNING -> WAITING -> RUNNING -> COMPLETED is valid.
    # A fresh user request starts a new run after completion.
    if requested not in _TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"invalid run transition: {current} -> {requested}")
