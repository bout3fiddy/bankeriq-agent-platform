"""Registered operations and model-generated tool calls."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..helpers.json_snapshot import _freeze_object
from ..types.json_value import JsonValue


# String enums give policies explicit choices with readable transport values.
# The registered operation supplies the policy enforced by the gateway.
class OutputPolicy(StrEnum):
    VISIBLE = "visible"
    RESTRICTED = "restricted"


# Registration groups policy, version and schema for the gateway's execution checks.
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Trusted registration for one operation; schema validation comes later."""

    name: str
    version: str
    description: str
    argument_schema: Mapping[str, JsonValue] = field(repr=False)
    output_policy: OutputPolicy
    required_permissions: frozenset[str] = frozenset()
    requires_approval: bool = False

    def __post_init__(self) -> None:

        # Normalise during construction before exposing the frozen value. Full
        # schema validation and permission evaluation belong to later components.
        object.__setattr__(self, "required_permissions", frozenset(self.required_permissions))
        object.__setattr__(self, "argument_schema", _freeze_object(self.argument_schema))


# The call ID correlates a result with its request. The runtime resolves the
# tool version from trusted registration.
@dataclass(frozen=True, slots=True)
class ToolCall:
    """Untrusted model request, evaluated using runtime identity and registration."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze_object(self.arguments))
