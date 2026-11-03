"""A persistent async worker used while its disposable local primary is stopped."""

import asyncio
import os
import sys
from pathlib import Path

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.state import RunStatus
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.queue import PostgresQueue
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery


def complete(context, state):
    if state.arguments.get("final"):
        return Completion(RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, "saved"),))

    return Completion(
        RunStatus.RUNNING,
        next_task=Recovery(
            kind="outage", operation_id="final", idempotent=True, arguments={"final": True}
        ),
    )


async def run() -> None:
    marker = Path(sys.argv[1])
    queue = PostgresQueue(PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"], pool_size=4))
    handlers = Handlers({("outage", 1): Handler(lambda c, s: True, complete, idempotent=True)})
    worker = AsyncWorker(queue, handlers, queue_name="outage", concurrency=1, poll_seconds=0.1)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_async(stop))
    marker.write_text("ready")

    while not marker.with_suffix(".stop").exists():
        if task.done():
            task.result()

        await asyncio.sleep(0.1)

    stop.set()
    await task
    queue.store.close()


if __name__ == "__main__":
    asyncio.run(run())
