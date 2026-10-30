"""Model-facing outcomes and internal artifact delivery references."""

from dataclasses import dataclass, field
from enum import StrEnum

from ..helpers.json_snapshot import _freeze_json
from ..types.json_value import JsonValue


# Approval replies and job completions need different continuation checks.
class WaitReason(StrEnum):
    APPROVAL = "approval"
    JOB = "job"


# Fixed codes let the model handle failures without receiving exception details
# that could contain records, credentials or internal endpoint names.
class ToolErrorCode(StrEnum):
    DENIED = "denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNAVAILABLE = "unavailable"
    EXECUTION_FAILED = "execution_failed"
    CANCELLED = "cancelled"


# This class identifies payloads admitted to model context by output enforcement.
# The constructor validates JSON structure; content checks belong to enforcement.
@dataclass(frozen=True, slots=True)
class VisibleResult:
    """Payload admitted to model context by output enforcement."""

    call_id: str
    payload: JsonValue = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_json(self.payload, frozenset()))


# A restricted receipt carries only the call ID and signals successful completion.
# Report locations travel through the authenticated delivery path.
@dataclass(frozen=True, slots=True)
class RestrictedReceipt:
    """Completion receipt for a successful restricted operation."""

    call_id: str


# A dedicated failure type gives callers a fixed code to handle. Limiting errors
# to these codes reduces the risk of exposing raw worker output.
@dataclass(frozen=True, slots=True)
class ToolFailure:
    """Fixed error codes avoid accidentally forwarding raw exception text."""

    call_id: str
    code: ToolErrorCode


# Dispatch returns this value when execution needs an approval or job completion.
# The opaque reference lets a later event identify the outstanding approval/job.
@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """Reference to an approval or job; matching and replay checks come later."""

    call_id: str
    reason: WaitReason
    reference_id: str


# Separate variants avoid combinations such as a restricted receipt with a
# visible payload. These aliases let type checkers track the supported variants.
type ToolResult = VisibleResult | RestrictedReceipt | ToolFailure | PendingToolCall


# Delivery receives an artifact ID separately from the model transcript and
# checks the authenticated user's access before serving the artifact.
@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Internal delivery metadata, deliberately excluded from ToolResult."""

    call_id: str
    artifact_id: str
