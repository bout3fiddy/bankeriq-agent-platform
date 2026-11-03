"""Killable application boundaries with a synthetic provider idempotency ledger."""

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import TERMINAL_STATUSES
from bankeriq_agent_platform.storage.postgres import PostgresStateStore
from bankeriq_agent_platform.workers.asynchronous import AsyncWorker
from bankeriq_agent_platform.workers.state import Handlers
from tests.integration.async_process import composition


def halt(marker: Path) -> None:
    marker.write_text("boundary reached")

    while True:
        time.sleep(1)


async def run() -> None:
    mode, phase, tenant, marker_name = sys.argv[1:]
    marker = Path(marker_name)
    ledger = marker.with_suffix(".effects")
    context = RunContext(tenant, "user", "chat", "run")
    store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"])
    runtime, handlers = composition(store)
    entries = dict(handlers.entries)
    original = entries["tool", 1]

    def execute(c, state):
        known = json.loads(ledger.read_text()) if ledger.exists() else {}
        known.setdefault(state.operation_id, {"total": 5})
        ledger.write_text(json.dumps(known))
        result = original.execute(c, state)

        if mode == "crash" and phase == "effect":
            halt(marker)

        return result

    entries["tool", 1] = replace(original, execute=execute)
    handlers = Handlers(entries)

    if mode == "resume":
        for _ in range(100):
            if store.load_run(context).run.status in TERMINAL_STATUSES:
                print("terminal", flush=True)
                return

            for kind in ("model_prepare", "model_infer", "tool"):
                lease = runtime.queue.claim(kind, handlers, seconds=2)

                if lease is not None:
                    await AsyncWorker(
                        runtime.queue, handlers, queue_name=kind, lease_seconds=2
                    ).execute_async(lease)

            await asyncio.sleep(0.01)

        raise RuntimeError("recovery did not terminate")

    if phase == "admission":
        store._fault = lambda event: halt(marker) if event == "after_commit" else None
        await runtime.admit(context, request_id="request", text="calculate")
        return

    kind = "model_infer" if phase in ("claim", "model_response", "final") else "tool"
    lease = runtime.queue.claim(kind, handlers, seconds=0.5)
    assert lease is not None

    if phase == "claim":
        halt(marker)

    prepared = runtime.queue.prepare(lease)
    handler = handlers.resolve(prepared)
    result = (
        await handler.async_execute(context, prepared)
        if handler.async_execute
        else handler.execute(context, prepared)
    )

    if phase == "model_response":
        halt(marker)

    store._fault = lambda event: halt(marker) if event == "after_commit" else None
    runtime.queue.settle(lease, result)


if __name__ == "__main__":
    asyncio.run(run())
