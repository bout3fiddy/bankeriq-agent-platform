"""Local backup/restore rehearsal with a synthetic external-effect and acceptance ledger."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate
from bankeriq_agent_platform.workers.queue import PostgresQueue
from bankeriq_agent_platform.workers.runner import Worker
from bankeriq_agent_platform.workers.state import Completion, Handler, Handlers, Recovery


def run(base: str, container: str, output: Path) -> None:
    source_name, restored_name = "source_" + uuid4().hex, "restored_" + uuid4().hex
    source_dsn = make_conninfo(base, dbname=source_name)
    restored_dsn = make_conninfo(base, dbname=restored_name)
    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])
    completed = Completion(RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, "effect saved"),))
    effect = Recovery(kind="effect", operation_id="effect-17", run_budget=16)
    context = RunContext("restore-tenant", "user", "chat", "run")

    try:
        with psycopg.connect(base, autocommit=True) as connection:
            for name in (source_name, restored_name):
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        migrate(source_dsn)
        store = PostgresStateStore(source_dsn)
        store.create_conversation(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )
        queue = PostgresQueue(store)
        queue.configure("prepare", concurrency=1, tenant_concurrency=1)
        queue.configure("effect", concurrency=1, tenant_concurrency=1)
        handlers = Handlers(
            {
                ("prepare", 1): Handler(
                    lambda c, s: True,
                    lambda c, s: Completion(RunStatus.RUNNING, next_task=effect),
                    idempotent=True,
                ),
                ("effect", 1): Handler(lambda c, s: True, lambda c, s: completed),
            }
        )
        queue.admit(
            context,
            request_id="request-17",
            items=(Message(MessageRole.USER, "effect"),),
            state=Recovery(kind="prepare", operation_id="prepare-17", idempotent=True),
        )
        lease = queue.claim("prepare", handlers)
        assert lease is not None
        Worker(queue, handlers, queue_name="prepare").execute(lease)
        dispatched = queue.claim("effect", handlers)
        assert dispatched is not None
        queue.prepare(dispatched)

        # Dispatch is paused here: no worker process is running during backup or
        # restore. The provider's later durable receipt survives outside this DB.
        backup = subprocess.run(
            ["docker", "exec", container, "pg_dump", "-U", "postgres", "-Fc", source_name],
            check=True,
            capture_output=True,
        ).stdout
        provider_ledger = {effect.operation_id: completed}
        queue.settle(dispatched, provider_ledger[effect.operation_id])
        late = RunContext(context.tenant_id, context.user_id, context.conversation_id, "late")
        late_state = Recovery(kind="prepare", operation_id="late-prepare", idempotent=True)
        late_items = (Message(MessageRole.USER, "accepted after backup"),)
        queue.admit(late, request_id="late-request", items=late_items, state=late_state)
        old_view = SessionService(store, cache).read(context)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                "postgres",
                "--no-owner",
                "-d",
                restored_name,
            ],
            input=backup,
            check=True,
            capture_output=True,
        )
        stale = PostgresStateStore(restored_dsn)
        new_generation = str(uuid4())

        with psycopg.connect(restored_dsn) as connection:
            connection.execute("UPDATE runtime_generation SET recovery_id=%s", (new_generation,))
            connection.execute(
                "UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second' "
                "WHERE status='leased'"
            )

        try:
            PostgresQueue(stale).renew(dispatched)
        except StateConflictError:
            pass
        else:
            raise AssertionError("old generation retained execution authority")

        restored = PostgresStateStore(restored_dsn)
        restored_queue = PostgresQueue(restored)
        executions = []

        def forbidden(c, state):
            executions.append(state.operation_id)
            raise AssertionError("unknown effect was replayed")

        lookups = []

        def lookup(c, state):
            lookups.append(state.operation_id)
            return provider_ledger.get(state.operation_id)

        recovery_handlers = Handlers(
            {("effect", 1): Handler(lambda c, s: True, forbidden, reconcile=lookup)}
        )
        assert restored_queue.claim("effect", recovery_handlers) is None

        with psycopg.connect(restored_dsn) as connection:
            connection.execute("UPDATE jobs SET due_at=clock_timestamp() WHERE status='reconcile'")

        recovered = restored_queue.claim("effect", recovery_handlers)
        assert recovered is not None
        Worker(restored_queue, recovery_handlers, queue_name="effect").execute(recovered)
        assert not executions and lookups == [effect.operation_id]
        assert restored.load_run(context).run.status == RunStatus.COMPLETED

        # Reconcile the acceptance ledger too. Absence in the restored DB alone
        # did not prove the later request was never acknowledged to its caller.
        restored_queue.admit(late, request_id="late-request", items=late_items, state=late_state)
        restored_queue.admit(late, request_id="late-request", items=late_items, state=late_state)
        sessions = SessionService(restored, cache)
        fresh = sessions.read(context)
        assert fresh["recovery_id"] != old_view["recovery_id"]
        cache.client.flushdb()
        assert sessions.read(context)["recovery_id"] == new_generation
        assert restored.load_run(late).run.status == RunStatus.QUEUED
        output.write_text(
            json.dumps(
                {
                    "logical_backup_bytes": len(backup),
                    "old_generation_fenced": True,
                    "external_effect_lookups": len(lookups),
                    "external_effect_replays": len(executions),
                    "post_backup_acceptance_recovered": True,
                    "redis_rebuilt": True,
                    "dispatch_paused_during_cutover": True,
                },
                indent=2,
            )
            + "\n"
        )
        print(output.read_text())
    finally:
        cache.close()

        with psycopg.connect(base, autocommit=True) as connection:
            for name in (source_name, restored_name):
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(os.environ["BANKERIQ_TEST_POSTGRES_DSN"], args.container, args.output)
