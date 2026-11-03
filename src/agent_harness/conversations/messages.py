"""Conversation text admitted through input policy."""

from dataclasses import dataclass, field
from enum import StrEnum


# Tool calls/results have their own types. The runtime supplies trusted system
# instructions separately from conversation text.
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


# Hide text from default repr to reduce accidental logging. Explicit serialization
# and admission to external model context require their own controls.
@dataclass(frozen=True, slots=True)
class Message:
    """Conversation text whose admission to model context requires input policy checks."""

    role: MessageRole
    text: str = field(repr=False)
