"""Exercise loopback streaming with isolated primary state and independent worker processes."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx2
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_runtime.storage.postgres import migrate
from tests.scaling.conversation_process import ANSWER
from tests.scaling.conversation_workload import cohort


async def main(args):
    primary = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]
    schema = "concurrency_" + uuid4().hex

    if args.output.exists():
        raise ValueError("use a new evidence directory")

    args.output.mkdir(parents=True)

    with psycopg.connect(primary) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    dsn = make_conninfo(primary, options=f"-c search_path={schema}", application_name=schema)
    children, logs = [], []

    try:
        migrate(dsn)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        for role in ("api", *("worker" for _ in range(args.workers))):
            log = (args.output / f"process-{len(children)}.log").open("w")
            logs.append(log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.scaling.conversation_process",
                        role,
                        "--port",
                        str(port),
                        "--slots",
                        str(args.slots),
                        "--prepare-slots",
                        str(args.prepare_slots if args.prepare_slots is not None else args.slots),
                        "--capacity",
                        str(args.capacity),
                    ],
                    env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )

        async def headers():
            return {"authorization": "Bearer synthetic-local"}

        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            deadline = monotonic() + 30

            while True:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("fixture process failed; inspect its bounded local log")

                try:
                    response = await client.get("/missing")

                    if response.status_code == 404:
                        break
                except httpx2.TransportError:
                    pass

                if monotonic() > deadline:
                    raise TimeoutError("local API startup timed out")

                await asyncio.sleep(0.05)

            for tier in (1, 2, 5, 10):
                record = await cohort(
                    client,
                    headers=headers,
                    output=args.output / f"tier-{tier}.json",
                    sessions=tier,
                    count=args.count,
                    interval=args.interval * tier,
                    expected=ANSWER,
                    collect_usage=False,
                )
                print(
                    json.dumps(
                        {
                            "tier": tier,
                            "summary": record["summary"],
                            "failed_or_unfinished": record["failed_or_unfinished"],
                            "peak_active": record["peak_active"],
                        }
                    ),
                    flush=True,
                )

            # Retain two idle samples after all streams close, so resource
            # growth is judged against drained processes rather than peak work.
            await asyncio.sleep(11)
    finally:

        # Stop only children started here, then drop only this synthetic schema.
        # Retain their metadata logs and ledgers for failures as well as successes.
        for child in children:
            if child.poll() is None:
                child.terminate()

        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

        for log in logs:
            log.close()

        with psycopg.connect(primary) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--slots", type=int, choices=[1, 2, 4, 5], default=2)
    parser.add_argument("--prepare-slots", type=int, choices=[1, 2, 4, 5])
    parser.add_argument("--capacity", type=int, choices=[4, 8], default=4)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--interval", type=float, default=0.5)
    asyncio.run(main(parser.parse_args()))
