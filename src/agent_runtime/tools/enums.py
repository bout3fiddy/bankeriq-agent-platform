"""Tool output policies, failure codes and waiting reasons."""

from enum import StrEnum


# String enums give policies explicit choices with readable transport values.
# The registered operation supplies the policy enforced by the gateway.
class OutputPolicy(StrEnum):
    VISIBLE = "visible"
    RESTRICTED = "restricted"


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
