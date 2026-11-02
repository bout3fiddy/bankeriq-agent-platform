"""Bind verified callers and validated commands to coordinator entry points."""

from collections.abc import Callable
from uuid import uuid4

from ..conversations.messages import Message, MessageRole
from ..coordinator.control import CoordinatorResult
from ..coordinator.runner import Coordinator
from ..durable.continuation import ContinuationEvent
from ..durable.handler import ContinuationHandler
from ..runs.context import RunContext
from ..storage.interface import StateStore
from ..tools.results import WaitReason
from ..types.json_value import JsonValue
from .access import Principal
from .requests import (
    AdmitInputRequest,
    CreateConversationRequest,
    HistoryReadRequest,
    InvalidInvocation,
    Invocation,
    SessionReadRequest,
    StartRunRequest,
)
from .sessions import SessionAccess


class RuntimeEndpoint:
    """Handle one authenticated request using the application's shared runtime."""

    def __init__(
        self,
        store: StateStore,
        coordinator: Coordinator,
        continuations: ContinuationHandler,
        *,
        prepare_input: Callable[[Principal, str], str],
        sessions: SessionAccess | None = None,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._continuations = continuations
        self._prepare_input = prepare_input
        self._sessions = sessions

    def invoke(self, principal: Principal, command: Invocation) -> dict[str, JsonValue]:
        """Create a conversation, start one turn, or resolve a trusted pending event."""

        # Example: invoke(Principal("tenant-a", "user-1"), create_command)
        # returns a fresh conversation ID at revision 0 owned by that principal.
        # The transport's parsed command cannot supply an alternate tenant/user.
        if isinstance(command, CreateConversationRequest):
            conversation = self._store.create_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=str(uuid4()),
            )
            return {
                "conversation_id": conversation.conversation_id,
                "revision": conversation.revision,
            }

        if isinstance(command, (SessionReadRequest, HistoryReadRequest, AdmitInputRequest)):
            if self._sessions is None:
                raise InvalidInvocation("session operations are not configured")

            context = RunContext(
                principal.tenant_id,
                principal.user_id,
                command.conversation_id,
                command.run_id if isinstance(command, AdmitInputRequest) else "",
            )

            if isinstance(command, SessionReadRequest):
                return self._sessions.read(context, minimum_cursor=command.minimum_cursor)

            if isinstance(command, HistoryReadRequest):
                return self._sessions.history_page(
                    context, after=command.after, limit=command.limit
                )

            text = self._prepare_input(principal, command.message)
            operation = (
                self._sessions.stage if command.action == "stage_input" else self._sessions.admit
            )
            return operation(context, request_id=command.request_id, text=text)

        # The caller selects conversation/run IDs. Storage checks ownership using
        # the authenticated principal on every start or resume operation.
        context = RunContext(
            principal.tenant_id, principal.user_id, command.conversation_id, command.run_id
        )

        if isinstance(command, StartRunRequest):
            self._store.load_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=command.conversation_id,
            )

            # A local fake can use prepare_input=lambda principal, text: text
            # for synthetic prompts. Production wiring must supply admission
            # policy; there is no default that forwards arbitrary chat input.
            text = self._prepare_input(principal, command.message)
            result = self._coordinator.start(
                context,
                Message(MessageRole.USER, text),
                expected_conversation_revision=command.expected_conversation_revision,
            )
        else:

            # This is an event notification. The resolver loads the saved
            # approval/job outcome; the body cannot supply a decision or result.
            result = self._continuations.resume(
                context,
                ContinuationEvent(
                    command.call_id, WaitReason(command.reason), command.reference_id
                ),
            )

        return _result_body(result)


def _result_body(result: CoordinatorResult) -> dict[str, JsonValue]:
    """Return separate answer, wait and delivery fields to the authenticated caller."""

    return {
        "conversation_id": result.run.context.conversation_id,
        "conversation_revision": result.conversation_revision,
        "run_id": result.run.context.run_id,
        "status": result.run.status.value,
        "answer": result.answer,
        "pending": [
            {
                "call_id": call.call_id,
                "reason": call.reason.value,
                "reference_id": call.reference_id,
            }
            for call in result.run.pending_calls
        ],
        "artifacts": [
            {"call_id": artifact.call_id, "artifact_id": artifact.artifact_id}
            for artifact in result.artifacts
        ],
    }
