"""Synthetic async composition shared by independent worker acceptance processes."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.control import RunLimits
from agent_harness.coordinator.model import ModelReply, ModelRequest
from agent_harness.memory.projection import HistoryPolicy
from agent_harness.memory.semantic import TokenBudget
from agent_harness.prompts.resolution import FixedPromptResolver
from agent_harness.tools.bindings import ExecutionOutput, ToolBinding
from agent_harness.tools.definitions import ToolCall
from agent_harness.tools.gateway import ToolGateway
from agent_harness.tools.registry import ToolRegistry
from agent_harness.tools.results import VisibleResult
from agent_platform.composition import compose_queued_runtime
from agent_runtime.memory.service import SemanticMemory
from agent_runtime.memory.storage import SemanticStore
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.coordinator import QueuedCoordinator
from agent_runtime.workers.groups import OrchestrationQueue
from agent_runtime.workers.state import Handlers

from .harness import CALCULATE, PERMISSIONS, FakeAccess
from .summary_model import Extractor


class AsyncModel:
    async def complete(self, request: ModelRequest) -> ModelReply:
        start = max(
            index
            for index, item in enumerate(request.history)
            if isinstance(item, Message) and item.role == MessageRole.USER
        )

        if any(isinstance(item, VisibleResult) for item in request.history[start:]):
            return ModelReply(answer="Synthetic total saved")

        prefix = "call" if start == 0 else "call-" + uuid4().hex
        return ModelReply(
            calls=tuple(
                ToolCall(f"{prefix}-{i}", "calculate", {"values": [2, 3]}) for i in range(3)
            )
        )


def composition(
    store: PostgresStateStore,
    *,
    fail_fast: bool = False,
    tenant_concurrency: int = 4,
    concurrency: int = 4,
):
    prompts = FixedPromptResolver(
        "Batch independent calls. Wait for inputs before dependent calls."
    )
    queue = OrchestrationQueue(store, prompts)

    for kind in ("model_prepare", "model_infer", "tool"):
        queue.configure(kind, concurrency=concurrency, tenant_concurrency=tenant_concurrency)

    registry = ToolRegistry((CALCULATE,))
    access = FakeAccess()
    access.permissions_for = lambda context: PERMISSIONS
    gateway = ToolGateway(
        registry,
        {
            ("calculate", "v1"): ToolBinding(
                lambda context, args: ExecutionOutput({"total": 5}),
                lambda args: None,
                visible_filter=lambda value: value,
            )
        },
        access,
    )
    counter = FoundryTokenCounter("gpt-4o")
    memory = SemanticMemory(
        SemanticStore(store),
        Extractor(),
        prompts=FixedPromptResolver("Memory format version 1."),
        summary_budget=TokenBudget("fixture-summary", 8192, 1024, counter),
        context_budget=TokenBudget("fixture-context", 8192, 1024, counter),
        access_for=lambda context: HistoryPolicy(
            "fixture-1", frozenset({"calculate"}), lambda fact: True
        ),
    )
    limits = RunLimits()
    runtime = compose_queued_runtime(
        store=store,
        registry=registry,
        gateway=gateway,
        model=AsyncModel(),
        permissions_for=access.permissions_for,
        prompts=prompts,
        limits=limits,
        bind=QueuedCoordinator.bind_tool,
        fail_fast=fail_fast,
        memory=memory,
    )
    handlers = dict(runtime.handlers().entries)

    # Synthetic calculation is pure, so saved and current replay policies agree.
    handlers["tool", 1] = replace(handlers["tool", 1], replay_allowed=True)
    original_bind = runtime.bind
    runtime.bind = lambda c, call, definition, state: replace(
        original_bind(c, call, definition, state),
        recovery=original_bind(c, call, definition, state).recovery.model_copy(
            update={"replay_allowed": True}
        ),
    )
    return runtime, Handlers(handlers)


async def main() -> None:
    store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"])
    runtime, handlers = composition(store)
    kind = sys.argv[1]
    marker = Path(sys.argv[2])
    release = Path(sys.argv[3])
    lease = runtime.queue.claim(kind, handlers, seconds=0.5)
    assert lease is not None
    runtime.queue.prepare(lease)
    marker.write_text(lease.job_id)

    # Parent tests hold two independent processes here to prove concurrent claims,
    # kill one, and release the other without a shared Python interpreter or pool.
    while not release.exists():
        runtime.queue.renew(lease, seconds=0.5)
        await asyncio.sleep(0.05)

    worker = AsyncWorker(runtime.queue, handlers, queue_name=kind, lease_seconds=0.5)
    await worker.execute_async(lease)
    print("settled", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
