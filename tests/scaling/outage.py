"""Stop/restart only the explicitly named disposable group-08 PostgreSQL container."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from bankeriq_agent_platform.storage.postgres import CommitUnknownError, PostgresStateStore, migrate
from bankeriq_agent_platform.workers.queue import PostgresQueue
from bankeriq_agent_platform.workers.state import Recovery


def run(base: str, container: str, output: Path) -> None:
    if container != "bankeriq-group08-postgres":
        raise ValueError("outage rehearsal only supports the disposable group-08 container")

    schema = "outage_" + uuid4().hex

    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    dsn = make_conninfo(base, options=f"-c search_path={schema}")
    migrate(dsn)
    store = PostgresStateStore(dsn)
    context = RunContext("tenant", "user", "chat", "run")
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    queue = PostgresQueue(store)
    queue.configure("outage", concurrency=1, tenant_concurrency=1)
    state = Recovery(kind="outage", operation_id="input", idempotent=True)
    items = (Message(MessageRole.USER, "synthetic input"),)

    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "ready"
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.scaling.outage_worker", str(marker)],
            env={**os.environ, "BANKERIQ_TEST_POSTGRES_DSN": dsn},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            for _ in range(100):
                if marker.exists():
                    break

                time.sleep(0.05)

            assert marker.exists()
            subprocess.run(
                ["docker", "stop", "--time", "1", container], check=True, capture_output=True
            )

            try:
                queue.admit(context, request_id="request", items=items, state=state)
            except CommitUnknownError:
                pass
            else:
                raise AssertionError("outage incorrectly acknowledged admission")

            time.sleep(1)
            assert child.poll() is None
            reconnect = time.monotonic()
            subprocess.run(["docker", "start", container], check=True, capture_output=True)

            for _ in range(100):
                try:
                    queue.admit(context, request_id="request", items=items, state=state)
                    break
                except CommitUnknownError:
                    time.sleep(0.1)
            else:
                raise AssertionError("primary reconnection failed")

            for _ in range(100):
                if store.load_run(context).run.status == RunStatus.COMPLETED:
                    break

                time.sleep(0.1)

            assert store.load_run(context).run.status == RunStatus.COMPLETED
            queue.admit(context, request_id="request", items=items, state=state)
            marker.with_suffix(".stop").touch()
            _, error = child.communicate(timeout=5)
            assert child.returncode == 0, error
            output.write_text(
                json.dumps(
                    {
                        "worker_survived_primary_outage": True,
                        "admission_acknowledged_during_outage": False,
                        "same_request_recovered": True,
                        "graceful_drain": True,
                        "restart_to_completion_seconds": time.monotonic() - reconnect,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(output.read_text())
        finally:
            subprocess.run(["docker", "start", container], check=True, capture_output=True)

            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

            with psycopg.connect(base) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(os.environ["BANKERIQ_TEST_POSTGRES_DSN"], args.container, args.output)
