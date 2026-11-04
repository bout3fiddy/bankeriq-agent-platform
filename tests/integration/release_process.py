"""One fresh worker process for actual queued release/rollback boundaries."""

import asyncio
import os
import sys

from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.state import Handlers

from .async_process import composition as tool_composition
from .semantic_process import composition as memory_composition


async def main() -> None:
    mode, kind, version = sys.argv[1:]
    store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"])

    if mode == "memory":
        runtime, handlers, _, _ = memory_composition(store, context_window=int(version))
    elif mode == "tool":
        runtime, handlers = tool_composition(store)
        entries = dict(handlers.entries)

        # The candidate worker supports another tool version. Persisted v1 intents
        # stay pending until the compatible v1 process drains them during rollback.
        # No v2 handler is executed or presented as a completed tool operation.
        entries["tool", int(version)] = entries.pop(("tool", 1))
        handlers = Handlers(entries)
    else:
        raise ValueError("unsupported release fixture mode")

    lease = runtime.queue.claim(kind, handlers, seconds=30)

    if lease is None:
        print("no_compatible_work", flush=True)
        return

    await AsyncWorker(runtime.queue, handlers, queue_name=kind).execute_async(lease)
    print("settled", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
