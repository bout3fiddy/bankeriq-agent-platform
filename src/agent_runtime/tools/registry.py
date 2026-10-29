"""Permission-filtered tool definitions for one runtime configuration."""

from collections.abc import Iterable
from types import MappingProxyType

from ._registration import _validate_registration
from .definitions import ToolDefinition


class ToolUnavailableError(LookupError):
    """The requested tool/version is unavailable under the supplied permissions."""


class ToolRegistry:
    """Snapshot selected tool versions; callers supply current trusted permissions."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        # One selected version per name makes model-generated names unambiguous.
        # Updating versions creates a new registry snapshot for subsequent runs.
        tools: dict[str, ToolDefinition] = {}

        for definition in definitions:
            _validate_registration(definition)

            if definition.name in tools:
                raise ValueError(f"duplicate tool name: {definition.name}")

            tools[definition.name] = definition

        self._tools = MappingProxyType(tools)

    def discover(self, granted_permissions: frozenset[str]) -> tuple[ToolDefinition, ...]:
        """Return authorised definitions in name order for reproducible model context."""

        return tuple(
            definition
            for _, definition in sorted(self._tools.items())
            if definition.required_permissions.issubset(granted_permissions)
        )

    def resolve(
        self,
        name: str,
        version: str,
        granted_permissions: frozenset[str],
    ) -> ToolDefinition:
        """Resolve a pinned version using permissions supplied for this lookup."""

        definition = self._tools.get(name)

        # Missing, changed and unauthorised definitions share one outward error
        # so a lookup cannot disclose the hidden catalogue. Resource-level checks
        # and approvals remain the gateway's responsibility.
        if (
            definition is None
            or definition.version != version
            or not definition.required_permissions.issubset(granted_permissions)
        ):
            raise ToolUnavailableError("tool unavailable")

        return definition
