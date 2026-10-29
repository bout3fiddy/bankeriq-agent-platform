"""Conversation roles and run lifecycle choices."""

from enum import StrEnum


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
