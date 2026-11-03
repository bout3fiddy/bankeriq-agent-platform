"""Compare repeated progress delivery at fixed payload size and growing retained history."""

import argparse
import json
import os
import statistics
import time
import tracemalloc
from typing import Any, cast
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.context import RunContext
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.sessions.stream import ProgressStream
from agent_runtime.storage.postgres import PostgresStateStore, migrate


class ObservedStore(PostgresStateStore):
    """Measure history reconstruction without changing queries or their results."""

    history_reads: int = 0

    def _recent_events(self, *args: Any, **kwargs: Any):
        self.history_reads += 1
        return super()._recent_events(*args, **kwargs)


def seed(store: PostgresStateStore, context: RunContext, turns: int) -> None:
    store.create_conversation(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
    )
    store.admit(context, request_id=context.run_id, items=(Message(MessageRole.USER, "input"),))

    # SQL setup fixes history size independently of execution concurrency. Every
    # synthetic old turn has two 512-byte events; the active input stays last.
    # The measured code still uses the public progress/session entry points.
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO runs (tenant_id,conversation_id,run_id,admission_sequence,active,"
            "status,checkpoint) SELECT %s,%s,'old-'||i,i+1,false,'completed',"
            "jsonb_build_object() FROM generate_series(1,%s) i",
            (context.tenant_id, context.conversation_id, turns),
        )
        connection.execute(
            "INSERT INTO conversation_events SELECT %s,%s,'old-'||((i+1)/2),i+1,"
            "'event-'||i,jsonb_build_object('type','Message','value',jsonb_build_object("
            "'role',CASE WHEN i%%2=1 THEN 'user' ELSE 'assistant' END,'text',repeat('x',512))) "
            "FROM generate_series(1,%s) i",
            (context.tenant_id, context.conversation_id, turns * 2),
        )
        connection.execute(
            "UPDATE runs SET admission_sequence=%s WHERE tenant_id=%s AND run_id=%s",
            (turns + 2, context.tenant_id, context.run_id),
        )
        connection.execute(
            "UPDATE conversations SET cursor=%s,admission_sequence=%s WHERE tenant_id=%s",
            (turns * 2 + 1, turns + 2, context.tenant_id),
        )
        connection.execute("ANALYZE")


def measure(dsn: str, redis_url: str, turns: int, iterations: int) -> dict[str, Any]:
    store = ObservedStore(dsn)
    context = RunContext(str(uuid4()), "user", "chat", "active")
    seed(store, context, turns)
    cache = RedisSessions(redis_url)
    service = SessionService(store, cache)
    service.read(context)
    stream = ProgressStream(service, context)

    # Warm imports, connections and Redis scripts before observing the same calls.
    for index in range(5):
        service.progress(context, sequence=index + 1, status="running")
        stream.poll()

        while stream.buffer.take() is not None:
            pass

    store.history_reads = 0
    tracemalloc.start()
    start_cpu = time.process_time()
    durations = []
    redis_before = cast(dict, cache.client.info("stats"))["total_net_input_bytes"]

    for index in range(iterations):
        started = time.perf_counter()
        service.progress(context, sequence=index + 6, status="running")
        stream.poll()

        while stream.buffer.take() is not None:
            pass

        durations.append((time.perf_counter() - started) * 1000)

    cpu = time.process_time() - start_cpu
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    redis_bytes = cast(dict, cache.client.info("stats"))["total_net_input_bytes"] - redis_before
    cache.close()
    return dict(
        turns=turns,
        iterations=iterations,
        median_ms=statistics.median(durations),
        p95_ms=sorted(durations)[int(len(durations) * 0.95)],
        cpu_seconds=cpu,
        history_reads=store.history_reads,
        retained_bytes=retained,
        peak_bytes=peak,
        redis_input_bytes_per_update=redis_bytes / iterations,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    base = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]
    schema = "probe_" + uuid4().hex

    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    dsn = make_conninfo(base, options=f"-c search_path={schema}")

    try:
        migrate(dsn)
        results = [
            measure(dsn, os.environ["BANKERIQ_TEST_REDIS_URL"], turns, args.iterations)
            for turns in (10, 300, 3000)
        ]
        from pathlib import Path

        Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))
    finally:
        with psycopg.connect(base) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
