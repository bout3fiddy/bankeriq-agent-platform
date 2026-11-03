"""Run independent roles, sample bounded state, and retain local measurement evidence."""

import argparse
import ast
import inspect
import json
import os
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_runtime.runs.context import RunContext
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.storage.history import ProjectionPolicy
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate
from bankeriq_agent_platform.workers.queue import PostgresQueue
from bankeriq_agent_platform.workers.scaling import pressure
from tests.integration.async_process import composition


def wrap_plan_text(value: Any) -> Any:
    """Split long EXPLAIN predicates losslessly to keep evidence source lines bounded.

    Joining text_chunks with an empty separator recovers the original SQL text.
    Every number, node and plan field is preserved.
    """

    if isinstance(value, str) and len(value) > 40:
        return {"text_chunks": [value[i : i + 40] for i in range(0, len(value), 40)]}

    if isinstance(value, dict):
        return {k: wrap_plan_text(v) for k, v in value.items()}

    if isinstance(value, list):
        return [wrap_plan_text(v) for v in value]

    return value


def distribution(values: list[float]) -> dict[str, float]:
    return (
        {
            "median": statistics.median(values),
            "p95": sorted(values)[int(len(values) * 0.95)],
            "max": max(values),
        }
        if values
        else {}
    )


def claim_plan(store: PostgresStateStore) -> Any:
    tree = ast.parse(textwrap.dedent(inspect.getsource(PostgresQueue.claim)))
    query = next(
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("SELECT j.*, c.user_id")
    )

    with store._connect() as connection:
        connection.execute("ANALYZE")
        row = connection.execute(
            ("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query).encode(), ("tool", [1], 2)
        ).fetchone()
        assert row is not None
        return row["QUERY PLAN"]


def rss(pids: list[int]) -> list[int]:
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", ",".join(map(str, pids))],
        capture_output=True,
        text=True,
        check=True,
    )
    return [int(line) * 1024 for line in result.stdout.splitlines()]


def run(base: str, redis_url: str, turns: int, output: Path) -> None:
    schema = "load_" + uuid4().hex

    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    dsn = make_conninfo(base, options=f"-c search_path={schema}")
    children: list[subprocess.Popen] = []
    migrate(dsn)
    store = PostgresStateStore(dsn, pool_size=4)
    runtime, handlers = composition(store, tenant_concurrency=2)
    sessions = SessionService(store, RedisSessions(redis_url, ttl_seconds=2))
    before_plan = claim_plan(store)

    with store._connect() as connection:
        connection.execute("CREATE TABLE probe_admitted (tenant text,run text,started timestamptz)")
        connection.execute(
            "CREATE TABLE probe_tokens (tenant text,run text,started timestamptz,"
            "PRIMARY KEY(tenant,run))"
        )
        wal_before = connection.execute("SELECT pg_current_wal_insert_lsn() AS lsn").fetchone()

    with tempfile.TemporaryDirectory(prefix="bankeriq-load-") as temporary:
        directory = Path(temporary)
        samples = []
        began = time.monotonic()

        try:
            for role in ("coordinator", "tool", "api"):
                for number in range(2):
                    log = (directory / f"{role}-{number}.log").open("w")
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "tests.scaling.load_process",
                            role,
                            str(number),
                            temporary,
                            str(turns // 2),
                        ],
                        env={
                            **os.environ,
                            "BANKERIQ_TEST_POSTGRES_DSN": dsn,
                            "BANKERIQ_TEST_REDIS_URL": redis_url,
                        },
                        stdout=log,
                        stderr=log,
                    )
                    log.close()
                    children.append(child)

            while time.monotonic() - began < 600:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError(
                        "load process exited: "
                        + "\n".join(path.read_text() for path in directory.glob("*.log"))
                    )

                with store._connect() as connection:
                    completed = connection.execute(
                        "SELECT tenant_id,count(*) AS n FROM runs "
                        "WHERE status='completed' GROUP BY tenant_id"
                    ).fetchall()
                    failed = connection.execute(
                        "SELECT count(*) AS n FROM runs WHERE status='failed'"
                    ).fetchone()
                    assert failed is not None and failed["n"] == 0, "load run failed"
                    lag = connection.execute(
                        "SELECT COALESCE(max(dirty_cursor-projected_cursor),0) "
                        "AS n FROM projection_work"
                    ).fetchone()
                    connections = connection.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE datname=current_database()"
                    ).fetchone()

                assert lag is not None and connections is not None
                samples.append(
                    {
                        "seconds": time.monotonic() - began,
                        "completed": completed,
                        "rss_bytes": rss([child.pid for child in children]),
                        "queues": {
                            kind: vars(pressure(runtime.queue, kind, handlers))
                            for kind in ("model_prepare", "model_infer", "tool")
                        },
                        "projection_lag_cursors": lag["n"],
                        "database_connections": connections["n"],
                    }
                )
                sessions.project_due(ProjectionPolicy(events=50), limit=10)

                if sum(row["n"] for row in completed) == turns:
                    break

                time.sleep(0.5)
            else:
                raise RuntimeError("load completion deadline exceeded")

            (directory / "stop").touch()

            for _ in range(100):
                if len(list(directory.glob("*.json"))) == 6:
                    break

                time.sleep(0.1)

            reports = {path.stem: json.loads(path.read_text()) for path in directory.glob("*.json")}
            assert len(reports) == 6
            after_drain = rss([child.pid for child in children])

            for report in reports.values():
                assert report.get("remaining_tasks", 0) == 0

            assert (
                max(after_drain) <= max(samples[min(5, len(samples) - 1)]["rss_bytes"]) + 67108864
            )
            context = RunContext("tenant-0", "user", "reused-0", "run-0")
            assert wal_before is not None
            read_times: dict[str, list[float]] = {"cold_ms": [], "warm_ms": []}

            for _ in range(10):
                sessions.cache.client.delete(sessions.cache.key(sessions.history.head(context)))

                for name in read_times:
                    started = time.perf_counter()
                    sessions.read(context)
                    read_times[name].append((time.perf_counter() - started) * 1000)

            with store._connect() as connection:
                counts = connection.execute(
                    "SELECT (SELECT count(*) FROM jobs WHERE recovery IS NOT NULL) AS jobs,"
                    "(SELECT count(*) FROM jobs WHERE status='leased') AS leased,"
                    "(SELECT count(*) FROM tool_groups WHERE joined) AS joins,"
                    "(SELECT count(*) FROM results) AS results"
                ).fetchone()
                tokens = connection.execute(
                    "SELECT extract(epoch FROM (t.started-a.started))*1000 "
                    "AS ms FROM probe_tokens t JOIN probe_admitted a "
                    "USING(tenant,run)"
                ).fetchall()
                wal = connection.execute(
                    "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s) AS bytes",
                    (wal_before["lsn"],),
                ).fetchone()
                tables = connection.execute(
                    "SELECT relname,n_tup_ins,n_tup_upd,n_live_tup,"
                    "pg_total_relation_size(relid) AS bytes FROM "
                    "pg_stat_user_tables WHERE schemaname=%s ORDER BY relname",
                    (schema,),
                ).fetchall()

            assert wal is not None
            assert counts == {"jobs": turns * 7, "leased": 0, "joins": turns, "results": turns * 3}

            for sample in samples:
                assert all(queue["leased"] <= 4 for queue in sample["queues"].values())

            plans = {"before": before_plan, "after": claim_plan(store)}
            time.sleep(2.1)
            assert (
                sessions.cache.client.get(sessions.cache.key(sessions.history.head(context)))
                is None
            )
            evidence = {
                "turns": turns,
                "elapsed_seconds": time.monotonic() - began,
                "counts": counts,
                "samples": samples,
                "processes": reports,
                "rss_after_drain": after_drain,
                "first_token_ms": distribution([float(row["ms"]) for row in tokens]),
                "reads": {name: distribution(values) for name, values in read_times.items()},
                "wal_bytes": int(wal["bytes"]),
                "tables": tables,
                "claim_plans": plans,
            }
            output.write_text(json.dumps(wrap_plan_text(evidence), indent=2, default=float) + "\n")
            print(
                json.dumps(
                    {
                        key: evidence[key]
                        for key in ("counts", "elapsed_seconds", "first_token_ms", "reads")
                    }
                )
            )
        finally:
            (directory / "exit").touch()
            (directory / "stop").touch()

            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

            sessions.cache.close()
            store.close()

            with psycopg.connect(base) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(
        os.environ["BANKERIQ_TEST_POSTGRES_DSN"],
        os.environ["BANKERIQ_TEST_REDIS_URL"],
        args.turns,
        args.output,
    )


if __name__ == "__main__":
    main()
