"""Independent synthetic worker/API processes for interrupted text-stream recovery."""

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_harness.coordinator.model import ModelReply
from agent_harness.runs.context import RunContext
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.publication import TextPublication
from agent_runtime.sessions.service import SessionService
from agent_runtime.sessions.stream import ProgressStream
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker

from .async_process import composition


async def main():
    mode, marker_name, encoded_context = sys.argv[1:]
    marker = Path(marker_name)
    context = RunContext(**json.loads(encoded_context))
    store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"], pool_size=2)
    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])
    sessions = SessionService(store, cache)

    try:
        if mode == "read":
            stream = ProgressStream(sessions, context)
            frames = []

            # Two polls on a fresh API instance still expose exactly one final.
            # Its primary read works after the parent removes this chat's cache.
            for _ in range(2):
                stream.poll()

                while (frame := stream.buffer.take()) is not None:
                    frames.append(frame.decode())

            print(json.dumps(frames), flush=True)
            return

        runtime, handlers = composition(store)
        lease = runtime.queue.claim("model_infer", handlers, seconds=30)
        assert lease is not None and lease.context == context
        text = "Interrupted draft" if mode == "crash" else "Recovered answer"

        class Model:
            async def stream(self, request, on_text):
                await on_text(text)

                # The parent kills only after actual Redis publication is visible.
                # A second process holds its replacement draft until the parent
                # verifies the changed ownership token, then settles normally.
                async with asyncio.timeout(20):
                    while True:
                        snapshot = await asyncio.to_thread(sessions.stream_snapshot, context)

                        if snapshot["text"] and snapshot["text"]["text"] == text:
                            marker.write_text(
                                json.dumps({"job": lease.job_id, "token": lease.token})
                            )
                            break

                        await asyncio.sleep(0.01)

                    while not marker.with_suffix(".finish").exists():
                        await asyncio.sleep(0.01)

                return ModelReply(answer=text)

        runtime.streaming = Model()
        runtime.publication = TextPublication(sessions, runtime.queue, allow=lambda _: True)
        await AsyncWorker(runtime.queue, handlers, queue_name="model_infer").execute_async(lease)
    finally:
        cache.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
