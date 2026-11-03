"""Measure synthetic local session reads/projection; requires disposable PostgreSQL and Redis."""

import json
import os
from statistics import median
from time import perf_counter
from uuid import uuid4

from agent_harness.runs.context import RunContext
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.sessions.service import SessionService
from agent_runtime.storage.postgres import PostgresStateStore, migrate


def main() -> None:
    dsn = os.environ["BANKERIQ_TEST_POSTGRES_DSN"]
    migrate(dsn)
    store = PostgresStateStore(dsn)
    cache = RedisSessions(os.environ["BANKERIQ_TEST_REDIS_URL"])
    sessions = SessionService(store, cache)
    context = RunContext("measure-" + uuid4().hex, "synthetic", "chat", "run")
    store.create_conversation(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
    )

    try:

        # One active run and 119 queued turns exercise a continuously active
        # conversation. Each request uses synthetic text; no model/tool executes.
        for index in range(120):
            turn = RunContext(
                context.tenant_id, context.user_id, context.conversation_id, str(index)
            )
            sessions.admit(turn, request_id=str(index), text=f"Synthetic request {index}")

        measured: dict[str, list[float]] = {"warm_read_ms": [], "cold_read_ms": [], "page_ms": []}
        head = sessions.history.head(context)
        started = perf_counter()
        sessions.history.materialize(context)
        sessions.history.materialize(context)
        projection_ms = 1000 * (perf_counter() - started)

        for _ in range(20):
            started = perf_counter()
            sessions.read(context)
            measured["warm_read_ms"].append(1000 * (perf_counter() - started))
            cache.client.delete(cache.key(head))
            started = perf_counter()
            sessions.read(context)
            measured["cold_read_ms"].append(1000 * (perf_counter() - started))
            started = perf_counter()
            sessions.history_page(context)
            measured["page_ms"].append(1000 * (perf_counter() - started))

        print(
            json.dumps(
                {
                    "events": 120,
                    "projection_ms": round(projection_ms, 2),
                    "measurements": {
                        key: {"median": round(median(values), 2), "maximum": round(max(values), 2)}
                        for key, values in measured.items()
                    },
                    "scope": "local synthetic sample; no production threshold or capacity claim",
                },
                indent=2,
            )
        )
    finally:
        cache.client.delete(cache.key(sessions.history.head(context)))
        store.delete_conversation(context)
        cache.close()


if __name__ == "__main__":
    main()
