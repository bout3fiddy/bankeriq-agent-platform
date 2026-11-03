"""Synthetic semantic memory composition, reusable in fresh worker processes."""

import asyncio
import os
import sys
from dataclasses import replace

from agent_runtime.conversations.messages import Message
from agent_runtime.conversations.semantic import TokenBudget
from agent_runtime.coordinator.model import ModelReply, ModelRequest
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry
from bankeriq_agent_platform.memory.retrieval import HISTORY_TOOL, HistoryRetrieval
from bankeriq_agent_platform.memory.service import HistoryPolicy, SemanticMemory
from bankeriq_agent_platform.memory.storage import SemanticStore
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from bankeriq_agent_platform.workers.asynchronous import AsyncWorker
from bankeriq_agent_platform.workers.state import Handlers

from .async_process import composition as base_composition
from .harness import FakeAccess
from .summary_model import Extractor


class Answer:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        text = " ".join(item.text for item in request.history if isinstance(item, Message))
        return ModelReply(
            answer="Remembered CHF; earlier EUR; deadline remains open"
            if "Use CHF instead of EUR" in text
            else "done"
        )


def composition(store: PostgresStateStore, *, context_window: int = 3000):
    runtime, _ = base_composition(store)
    counter = FoundryTokenCounter("gpt-4o")
    extractor = Extractor()
    memory = SemanticMemory(
        SemanticStore(store),
        extractor,
        prompts=FixedPromptResolver("Memory format version 1."),
        summary_budget=TokenBudget("gpt-4o-summary-v1", 5000, 512, counter),
        context_budget=TokenBudget("gpt-4o-context-v1", context_window, 256, counter),
        access_for=lambda context: HistoryPolicy(
            "policy-1", frozenset({"calculate", "retrieve_history"}), lambda f: True
        ),
    )
    runtime.memory = memory
    runtime.model = Answer()
    registry = ToolRegistry((HISTORY_TOOL,))
    access = FakeAccess()
    access.permissions_for = lambda context: frozenset({"history.read"})
    gateway = ToolGateway(
        registry, {("retrieve_history", "1"): HistoryRetrieval(memory).binding()}, access
    )
    runtime.gateway = gateway
    runtime.coordinator._registry = registry
    runtime.coordinator._permissions_for = access.permissions_for
    handlers = dict(runtime.handlers().entries)
    handlers["tool", 1] = replace(handlers["tool", 1], idempotent=True)
    original = runtime.bind
    runtime.bind = lambda *args: replace(
        original(*args), recovery=original(*args).recovery.model_copy(update={"idempotent": True})
    )
    return runtime, Handlers(handlers), memory, extractor


async def main() -> None:
    runtime, handlers, _, _ = composition(
        PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"])
    )
    kind = sys.argv[1]
    lease = runtime.queue.claim(kind, handlers, seconds=10)
    assert lease is not None
    await AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(lease)


if __name__ == "__main__":
    asyncio.run(main())
