"""Deterministic inference for exercising startup without cloud access."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import uuid4

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.model import ModelClient, ModelReply, ModelRequest
from agent_runtime.providers.models import ModelSettings
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import RestrictedReceipt, VisibleResult


class DemoModel:
    """Choose the next step from this request's history, allowing independent sessions."""

    def complete(self, request: ModelRequest) -> ModelReply:
        turn_start = max(
            index
            for index, item in enumerate(request.history)
            if isinstance(item, Message) and item.role is MessageRole.USER
        )
        results = [
            item
            for item in request.history[turn_start:]
            if isinstance(item, (VisibleResult, RestrictedReceipt))
        ]
        call_id = str(uuid4())

        if not results:
            return ModelReply(calls=(ToolCall(call_id, "sample_values", {}),))

        latest = results[-1]

        if isinstance(latest, RestrictedReceipt):
            return ModelReply(answer="The synthetic total is 42. The report is ready.")

        if isinstance(latest.payload, Mapping) and "values" in latest.payload:
            return ModelReply(
                calls=(ToolCall(call_id, "calculate", {"values": latest.payload["values"]}),)
            )

        return ModelReply(calls=(ToolCall(call_id, "build_report", {"total": 42}),))


@contextmanager
def open_demo_model(settings: ModelSettings) -> Iterator[ModelClient]:
    if settings.reasoning_effort is not None:
        raise ValueError("the mock model has no reasoning setting")

    yield DemoModel()
