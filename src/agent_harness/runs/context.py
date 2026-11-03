"""Verified identity bound to one coordinator run."""

from dataclasses import dataclass


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
