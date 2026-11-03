"""Bounded source records passed from persistence to memory policy."""

from dataclasses import dataclass, field
from typing import Any

from agent_harness.memory.semantic import MemoryFact
from agent_harness.storage.interface import Checkpoint


@dataclass(frozen=True)
class MemoryWindow:
    """A fixed prepared-step boundary and a bounded sequence of whole turns.

    covered_turn is essential: queued turn B can have a lower event cursor than
    turn A's final answer. Covering A never covers B. Both admission and event
    cursors constrain reads, so a later accepted message cannot leak into A.
    """

    checkpoint: Checkpoint
    cursor: int
    active_turn: int
    version: int
    covered_turn: int
    covered_cursor: int
    facts: tuple[MemoryFact, ...] = field(repr=False)
    turns: tuple[dict[str, Any], ...] = field(repr=False)
    active: dict[str, Any] = field(repr=False)
    has_more: bool
    policy_key: str
    partial_turn: bool = False
    retained_cursor: int = 0
