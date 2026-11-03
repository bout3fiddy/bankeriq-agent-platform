"""Independent API/coordinator/tool roles for a fixed-size sustained workload."""

import asyncio
import gc
import json
import os
import resource
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx2

from agent_harness.runs.context import RunContext
from agent_platform.api.queued import create_async_app
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.state import Handlers
from tests.integration.async_process import composition


async def main() -> None:
    role, number, directory, count = sys.argv[1:]
    output = Path(directory)
    store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"], pool_size=4)
    runtime, handlers = composition(store, tenant_concurrency=2)
    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"], ttl_seconds=2)
    sessions = SessionService(store, cache)
    result = {}
    started_cpu = time.process_time()

    if role == "api":
        tenant = "tenant-" + number

        for index in range(4):
            store.create_conversation(
                tenant_id=tenant, user_id="user", conversation_id=f"reused-{index}"
            )

        async def authenticate(request):
            index = int(request.headers["x-synthetic-index"])
            chat = f"reused-{index % 4}" if index % 2 == 0 else f"new-{index}"
            return RunContext(tenant, "user", chat, f"run-{index}")

        app = create_async_app(
            runtime, sessions, authenticate=authenticate, prepare_input=lambda c, text: text
        )
        latencies = []

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://synthetic"
        ) as client:
            for index in range(int(count)):
                if index % 2:
                    await asyncio.to_thread(
                        store.create_conversation,
                        tenant_id=tenant,
                        user_id="user",
                        conversation_id=f"new-{index}",
                    )

                with store._connect() as connection:
                    connection.execute(
                        "INSERT INTO probe_admitted VALUES (%s,%s,clock_timestamp())",
                        (tenant, f"run-{index}"),
                    )

                start = time.perf_counter()
                response = await client.post(
                    "/runs",
                    headers={"x-synthetic-index": str(index)},
                    json={"request_id": f"request-{index}", "text": "calculate " + "x" * 128},
                )
                assert response.status_code == 202, response.text
                latencies.append((time.perf_counter() - start) * 1000)

        result["admission_ms"] = latencies
    else:
        entries = dict(handlers.entries)
        original_model = entries["model_infer", 1]
        original_tool = entries["tool", 1]

        async def infer(context, state):
            with store._connect() as connection:
                connection.execute(
                    "INSERT INTO probe_tokens VALUES (%s,%s,clock_timestamp()) "
                    "ON CONFLICT DO NOTHING",
                    (context.tenant_id, context.run_id),
                )

            await asyncio.to_thread(
                sessions.progress,
                context,
                sequence=state.arguments["step"],
                status="running",
                model_tokens="synthetic",
            )
            await asyncio.sleep(0.01)
            assert original_model.async_execute is not None
            return await original_model.async_execute(context, state)

        async def tool(context, state):
            await asyncio.sleep(0.01)
            return original_tool.execute(context, state)

        entries["model_infer", 1] = replace(original_model, async_execute=infer)
        entries["tool", 1] = replace(original_tool, async_execute=tool)
        handlers = Handlers(entries)
        stop = asyncio.Event()
        kinds = ("model_prepare", "model_infer") if role == "coordinator" else ("tool",)
        tasks = [
            asyncio.create_task(
                AsyncWorker(
                    runtime.queue,
                    handlers,
                    queue_name=kind,
                    concurrency=2,
                    lease_seconds=5,
                    poll_seconds=0.05,
                    process_limit=6,
                    connection_budget=56,
                    api_reserve=8,
                ).run_async(stop)
            )
            for kind in kinds
        ]

        while not (output / "stop").exists():
            if any(task.done() for task in tasks):
                for task in tasks:
                    if task.done():
                        task.result()

            await asyncio.sleep(0.1)

        stop.set()
        await asyncio.gather(*tasks)
        gc.collect()
        result["remaining_tasks"] = len(asyncio.all_tasks()) - 1

    result.update(
        cpu_seconds=time.process_time() - started_cpu,
        max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )
    assert store._connections.pool is not None
    result["pool_stats"] = store._connections.pool.get_stats()
    store.close()
    cache.close()
    (output / f"{role}-{number}.json").write_text(json.dumps(result, indent=2) + "\n")

    # Remain alive after drain so the parent samples current resident memory.
    while not (output / "exit").exists():
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
