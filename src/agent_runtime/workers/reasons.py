"""Fixed worker failure codes; no exception text belongs in inspection or audit."""

from enum import StrEnum


class FailureReason(StrEnum):
    NONE = "none"
    PROCESS_LOST = "process_lost"
    CRASH_LIMIT = "crash_limit"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
    UNKNOWN_EFFECT = "unknown_effect"
    EXECUTION_RETRY = "execution_retry"
    APPROVAL_EXPIRED = "approval_expired"
    ACCESS_DENIED = "access_denied"
    APPROVAL_DENIED = "approval_denied"
    CANCELLED = "cancelled"
    HANDLER_UNAVAILABLE = "handler_unavailable"
    LOOKUP_UNAVAILABLE = "lookup_unavailable"
