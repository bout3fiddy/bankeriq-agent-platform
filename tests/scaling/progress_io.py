"""Supplement timing with primary SQL/buffer/WAL and bounded Redis write measurements."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from agent_runtime.runs.context import RunContext
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.sessions.stream import ProgressStream
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate
from tests.scaling.progress_probe import seed

base = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]
schema = "io_" + uuid4().hex

with psycopg.connect(base) as connection:
    connection.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public")
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

dsn = make_conninfo(base, options=f"-c search_path={schema}")
migrate(dsn)
store = PostgresStateStore(dsn, pool_size=4)
cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"], ttl_seconds=2)
reports = []

try:
    for turns in (10, 3000):
        context = RunContext(str(uuid4()), "user", "chat", "run")
        seed(store, context, turns)
        service = SessionService(store, cache)
        stream = ProgressStream(service, context)

        for _ in range(100):
            stream.poll()

            while stream.buffer.take() is not None:
                pass

            if stream.cursor is not None and int(stream.cursor.sequence) == turns * 2 + 1:
                break

        with psycopg.connect(base) as connection:
            connection.execute("SELECT public.pg_stat_statements_reset()")

        cpu = time.process_time()
        before = cast(dict, cache.client.info("stats"))["total_net_input_bytes"]

        for sequence in range(1, 101):
            service.progress(context, sequence=sequence, status="running")
            stream.poll()

            while stream.buffer.take() is not None:
                pass

        cpu = time.process_time() - cpu
        inbound = cast(dict, cache.client.info("stats"))["total_net_input_bytes"] - before

        with psycopg.Connection[dict[str, Any]].connect(base, row_factory=dict_row) as connection:
            statistics = connection.execute(
                "SELECT sum(calls) FILTER (WHERE query LIKE 'SELECT%') AS read_calls,"
                "sum(wal_bytes) AS wal_bytes,sum(shared_blks_hit) AS shared_hits,"
                "sum(shared_blks_read) AS shared_reads FROM public.pg_stat_statements "
                "WHERE query NOT LIKE '%pg_stat_statements%'"
            ).fetchone()

        assert statistics is not None
        progress = cache.progress(service.history.head(context))
        assert len(progress) == 32
        reports.append(
            {
                "turns": turns,
                "iterations": 100,
                "cpu_seconds": cpu,
                "postgres_per_update": {k: float(v or 0) / 100 for k, v in statistics.items()},
                "redis_input_bytes_per_update": inbound / 100,
                "retained_progress_entries": len(progress),
            }
        )

    Path(sys.argv[1]).write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports, indent=2))
finally:
    store.close()
    cache.close()

    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
