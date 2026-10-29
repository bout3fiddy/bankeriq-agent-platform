"""Explicit policy, message and lifecycle choices."""

from enum import StrEnum


# String enums give policies explicit choices with readable transport values.
# The registered operation supplies the policy enforced by the gateway.
class OutputPolicy(StrEnum):
    VISIBLE = "visible"
    RESTRICTED = "restricted"


# Tool calls/results have their own types. The runtime supplies trusted system
# instructions separately from conversation text.
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


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
