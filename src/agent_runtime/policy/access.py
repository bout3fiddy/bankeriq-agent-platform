"""External access decisions required before a tool executes."""

from typing import Protocol

from ..runs.context import RunContext
from ..tools.definitions import ToolCall, ToolDefinition


class AccessPolicy(Protocol):
    """Injected permission, resource-access and approval provider."""

    def permissions_for(self, context: RunContext) -> frozenset[str]:
        """Read current permissions for the verified tenant and user."""

        ...

    def can_access(self, context: RunContext, definition: ToolDefinition, call: ToolCall) -> bool:
        """Check operation and resource access using the validated arguments."""

        ...

    def consume_approval(
        self, context: RunContext, definition: ToolDefinition, call: ToolCall
    ) -> bool:
        """Atomically consume a matching, unexpired approval for this exact call."""

        ...

    # For example, approval for run-1/call-1/report-v2 with customer A must fail
    # for customer B or a different user. The provider owns storage and replay
    # protection; it checks context, version and arguments before consumption.
