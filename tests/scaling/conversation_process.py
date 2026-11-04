"""Local-only API/worker processes for concurrent streaming acceptance workloads."""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from contextlib import AsyncExitStack, suppress
from uuid import uuid4

import uvicorn
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_harness.coordinator.model import ModelReply
from agent_harness.runs.context import RunContext
from agent_platform.api.queued import create_async_app
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.publication import TextPublication
from agent_runtime.sessions.service import SessionService
from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.telemetry import configure
from agent_runtime.workers.asynchronous import AsyncWorker
from agent_runtime.workers.runner import Listener
from tests.integration.async_process import composition

ANSWER = "Synthetic answer delivered through the durable streaming runtime."


class Model:
    async def complete(self, request):
        await asyncio.sleep(0.1)
        return ModelReply(answer=ANSWER)

    async def stream(self, request, on_text):
        for part in ("Synthetic answer ", "delivered through the ", "durable streaming runtime."):
            await asyncio.sleep(0.04)
            await on_text(part)

        return ModelReply(answer=ANSWER)


async def main(args):
    configure()
    dsn = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]

    async with AsyncExitStack() as stack:
        connections = PrimaryConnections(dsn, size=4)
        stack.callback(connections.close)
        store = PostgresStateStore(dsn, connections=connections)
        runtime, handlers = composition(
            store, tenant_concurrency=args.capacity, concurrency=args.capacity
        )
        model = Model()
        runtime.model = model
        cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])
        stack.callback(cache.close)
        sessions = SessionService(store, cache)
        runtime.streaming = model
        runtime.publication = TextPublication(sessions, runtime.queue, allow=lambda c: True)

        # The workload fixture samples each process independently of chat count.
        # A single five-second sampler records current RSS, CPU time, Python
        # threads/tasks and pool counters. It never renews conversation activity.
        # For example, five connected readers add API tasks but no worker leases;
        # after they close, the task and pool counts can be compared with baseline.
        monitor = asyncio.create_task(resource_samples(connections, args.role))
        stack.push_async_callback(stop_monitor, monitor)

        if args.role == "api":

            # These explicit synthetic credentials are accepted only by this
            # loopback fixture. The hosted application continues using Entra.
            def identity(request):
                if request.headers.get("authorization") != "Bearer synthetic-local":
                    raise HTTPException(401)

                return request.headers.get("x-test-tenant", "tenant"), "operator"

            async def authenticate(request):
                tenant, user = identity(request)
                return RunContext(
                    tenant, user, request.headers["x-conversation-id"], request.headers["x-run-id"]
                )

            async def create(request):
                tenant, user = identity(request)
                chat = str(uuid4())
                await asyncio.to_thread(
                    store.create_conversation, tenant_id=tenant, user_id=user, conversation_id=chat
                )
                return JSONResponse({"conversation_id": chat})

            async def inspect(request):
                return JSONResponse(
                    await asyncio.to_thread(sessions.inspect, await authenticate(request))
                )

            app = create_async_app(
                runtime, sessions, authenticate=authenticate, prepare_input=lambda c, text: text
            )
            app.routes.extend(
                [Route("/conversations", create, methods=["POST"]), Route("/inspect", inspect)]
            )
            await uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"
                )
            ).serve()
            return

        stop = asyncio.Event()

        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        # A separate preparation limit isolates the hosted five-session bottleneck.
        # For example, compare two/five preparers with five inference slots in both
        # runs. Omission preserves the earlier equal-capacity fixture behaviour.
        preparers = args.slots if args.prepare_slots is None else args.prepare_slots
        await asyncio.gather(
            *(
                AsyncWorker(
                    runtime.queue,
                    handlers,
                    queue_name=kind,
                    concurrency=preparers if kind == "model_prepare" else args.slots,
                    poll_seconds=0.25,
                    lease_seconds=3,
                    listener=Listener(dsn),
                    process_limit=3,
                    connection_budget=64,
                ).run_async(stop)
                for kind in ("model_prepare", "model_infer", "tool")
            )
        )


async def resource_samples(connections, role):
    while True:
        rss = await asyncio.to_thread(
            subprocess.check_output,
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            text=True,
        )
        print(
            json.dumps(
                {
                    "event": "fixture.resources",
                    "time_unix": time.time(),
                    "role": role,
                    "pid": os.getpid(),
                    "rss_kib": int(rss.strip()),
                    "cpu_seconds": time.process_time(),
                    "python_threads": threading.active_count(),
                    "async_tasks": len(asyncio.all_tasks()),
                    "pool": connections.pool.get_stats(),
                }
            ),
            flush=True,
        )
        await asyncio.sleep(5)


async def stop_monitor(task):
    task.cancel()

    with suppress(asyncio.CancelledError):
        await task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--port", type=int, default=18088)
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--prepare-slots", type=int, choices=[1, 2, 4, 5])
    parser.add_argument("--capacity", type=int, choices=[4, 8], default=4)
    asyncio.run(main(parser.parse_args()))
