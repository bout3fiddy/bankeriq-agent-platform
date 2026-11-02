"""Application-provided session operations; core owns no database or cache client."""

from typing import Protocol

from ..runs.context import RunContext
from ..types.json_value import JsonValue


class SessionAccess(Protocol):

    # open_runtime(sessions=service) enables display and admission commands.
    # service.read() may use Redis, but service.admit() must commit durable input
    # and queued work before returning accepted=True. These commands never invoke
    # the coordinator; worker scheduling is a separate application responsibility.
    def read(self, context: RunContext, *, minimum_cursor: int = 0) -> dict[str, JsonValue]: ...

    def history_page(
        self, context: RunContext, *, after: int = 0, limit: int = 100
    ) -> dict[str, JsonValue]: ...

    def stage(self, context: RunContext, *, request_id: str, text: str) -> dict[str, JsonValue]: ...

    def admit(self, context: RunContext, *, request_id: str, text: str) -> dict[str, JsonValue]: ...
