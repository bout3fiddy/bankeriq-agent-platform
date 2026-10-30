"""Validate model replies before the coordinator acts on them."""

from .model import ModelReply


def is_valid_reply(reply: ModelReply, previous_call_ids: set[str]) -> bool:
    """Require one next action and unique tool-call IDs across the conversation."""

    has_answer = reply.answer is not None
    has_calls = bool(reply.calls)

    # This reply format supports an answer or tool requests. Commentary alongside
    # tool calls will need a richer representation when the provider adapter is built.
    if has_answer and has_calls:
        return False

    if not has_answer and not has_calls:
        return False

    call_ids = [call.call_id for call in reply.calls]
    unique_call_ids = set(call_ids)

    # Example: ["call-1", "call-1"] would make result matching ambiguous.
    if len(call_ids) > len(unique_call_ids):
        return False

    # Reusing an ID already in history could repeat an action under the same ID.
    reused_call_ids = previous_call_ids.intersection(unique_call_ids)

    return len(reused_call_ids) == 0
