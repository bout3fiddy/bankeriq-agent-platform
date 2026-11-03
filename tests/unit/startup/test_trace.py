"""Keep human-readable traces faithful to model batches and tool feedback."""

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.results import ToolErrorCode, ToolFailure, VisibleResult
from tests.validation.trace import explain_history


def test_multiple_tools_in_one_reply_share_a_model_call() -> None:
    trace = explain_history(
        (
            Message(MessageRole.USER, "Read two public samples."),
            ToolCall("first", "sample_a", {}),
            ToolCall("second", "sample_b", {}),
            VisibleResult("second", {"value": 30}),
            VisibleResult("first", {"value": 12}),
            Message(MessageRole.ASSISTANT, "The samples contain 12 and 30."),
        )
    )

    first_call, second_call = trace.split("### Model call 2")
    assert "### Model call 1" in first_call
    assert "sample_a({})" in first_call and "sample_b({})" in first_call
    assert "### Model call 3" not in trace
    assert '`sample_a` returned `{"value": 12}`' in second_call
    assert '`sample_b` returned `{"value": 30}`' in second_call


def test_failure_is_fed_back_without_claiming_tool_success() -> None:
    trace = explain_history(
        (
            Message(MessageRole.USER, "Calculate a total."),
            ToolCall("first", "calculate", {"values": [12, 30]}),
            ToolFailure("first", ToolErrorCode.DENIED),
            Message(MessageRole.ASSISTANT, "Access was denied."),
        )
    )

    next_call = trace.split("### Model call 2")[1]
    assert "`calculate` failed with code `denied`" in next_call
    assert "Access was denied." in next_call
    assert "**Tool result:**" not in trace
