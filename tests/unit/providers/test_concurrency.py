"""Summary capacity is bounded and cancellation releases its reservation."""

import asyncio

from agent_harness.coordinator.model import ModelRateLimited, ModelReply, ModelRequest
from agent_runtime.providers.concurrency import LimitedAsyncModelClient


def test_summary_waiters_do_not_reserve_ordinary_model_capacity():
    async def run():
        release = asyncio.Event()
        active, peak = 0, 0

        class Model:
            async def complete(self, request):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)

                try:
                    await release.wait()
                    return ModelReply(answer="done")
                finally:
                    active -= 1

        model = Model()
        summaries = LimitedAsyncModelClient(model, concurrency=2)
        request = ModelRequest((), ())
        tasks = [asyncio.create_task(summaries.complete(request)) for _ in range(5)]
        await asyncio.sleep(0)
        assert active == 2

        # Ordinary inference owns a worker slot and calls the shared adapter
        # directly. Summary waiters occupy neither its semaphore nor a thread.
        ordinary = asyncio.create_task(model.complete(request))
        await asyncio.sleep(0)
        assert active == peak == 3
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await asyncio.sleep(0)
        assert active == 3
        release.set()
        replies = await asyncio.gather(*tasks[1:], ordinary)
        assert all(reply.answer == "done" for reply in replies)
        assert active == 0

    asyncio.run(run())


def test_real_rate_limit_is_propagated_for_durable_retry():
    async def run():
        error = ModelRateLimited(3)

        class Model:
            async def complete(self, request):
                raise error

        model = LimitedAsyncModelClient(Model(), concurrency=1)

        for _ in range(2):
            try:
                await model.complete(ModelRequest((), ()))
            except ModelRateLimited as caught:
                assert caught is error
            else:
                raise AssertionError("provider throttle was swallowed")

    asyncio.run(run())
