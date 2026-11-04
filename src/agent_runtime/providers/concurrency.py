"""Application-owned concurrency bound for calls sharing one provider workload."""

import asyncio
from time import monotonic

from agent_harness.coordinator.model import AsyncModelClient, ModelReply, ModelRequest
from agent_runtime.telemetry import ACTIVE_RUN, emit


class LimitedAsyncModelClient:
    """Reserve a bounded number of active provider calls inside one coordinator.

    Startup wraps the summarizer with concurrency=2 while ordinary inference
    uses its independent worker capacity. A waiting summary uses an async task,
    with no sleep-based token allowance or extra thread. Provider 429s still reach
    the durable worker unchanged. Replica count bounds the aggregate call ceiling.
    """

    def __init__(self, model: AsyncModelClient, *, concurrency: int) -> None:
        if not 1 <= concurrency <= 64:
            raise ValueError("invalid provider concurrency")

        self.model = model
        self.slots = asyncio.Semaphore(concurrency)

    async def complete(self, request: ModelRequest) -> ModelReply:
        started = monotonic()

        async with self.slots:
            emit(
                "model.capacity_wait",
                ACTIVE_RUN.get(),
                elapsed_ms=round((monotonic() - started) * 1000),
                summary=True,
            )
            return await self.model.complete(request)
