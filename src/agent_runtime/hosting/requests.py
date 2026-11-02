"""Validate external invocation payloads before they reach runtime logic."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class InvocationRequest(BaseModel):
    """Common parsing rules: require explicit types and reject unknown fields."""

    # A body containing user_id, permissions or tools must fail parsing. Startup
    # supplies those authorities through authenticated identity and the registry.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CreateConversationRequest(InvocationRequest):
    action: Literal["create_conversation"]


class StartRunRequest(InvocationRequest):
    action: Literal["start_run"]
    conversation_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    expected_conversation_revision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=32_000, repr=False)


class ResumeRunRequest(InvocationRequest):
    action: Literal["resume_run"]
    conversation_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    call_id: str = Field(min_length=1, max_length=256)
    reason: Literal["approval", "job"]
    reference_id: str = Field(min_length=1, max_length=256)


type Invocation = CreateConversationRequest | StartRunRequest | ResumeRunRequest

# The action selects the matching request class. For example, start_run requires
# a message and a revision; resume_run requires a matching pending-event reference.
# TypeAdapter applies these rules to JSON without constructing a wrapper object.
_REQUEST_PARSER = TypeAdapter(Annotated[Invocation, Field(discriminator="action")])


class InvalidInvocation(ValueError):
    """Malformed or unsupported request; raw validation details stay internal."""


def parse_invocation(body: bytes) -> Invocation:

    # parse_invocation(b'{"action":"create_conversation"}') returns the typed
    # creation command. Adding "permissions":["reports.create"] is rejected
    # before the endpoint sees it; strict parsing also rejects revision "3"
    # when the start command requires an integer revision.
    try:
        return _REQUEST_PARSER.validate_json(body)
    except ValidationError:

        # Pydantic errors include offending input. Return a fixed failure so a
        # malformed message or spoofed identity cannot escape into HTTP errors.
        raise InvalidInvocation("invalid invocation") from None
