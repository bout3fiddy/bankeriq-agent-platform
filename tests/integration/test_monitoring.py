"""Queue observations include never-claimed work without renewing conversation activity."""

import asyncio

from agent_runtime.monitoring import queue_ages

from .async_process import composition
from .test_postgres import context as context
from .test_postgres import store as store
from .test_sessions import dsn as dsn
from .test_sessions import primary_dsn as primary_dsn
from .test_sessions import redis_url as redis_url


def test_due_probe_ignores_future_and_cancelled_work_and_is_passive(store, context):
    runtime, _ = composition(store)
    asyncio.run(runtime.admit(context, request_id="monitored", text="Synthetic test input."))

    with store._connect() as connection:
        before = connection.execute("SELECT last_activity FROM conversations").fetchone()
        connection.execute("UPDATE jobs SET due_at=clock_timestamp()-interval '40 seconds'")

    assert 40_000 <= queue_ages(store)["model_prepare"] < 45_000

    with store._connect() as connection:
        after = connection.execute("SELECT last_activity FROM conversations").fetchone()
        assert before == after
        assert connection.execute("SELECT attempt FROM jobs").fetchone()["attempt"] == 0
        connection.execute("UPDATE jobs SET status='stopped',effect_pending=true")

    # An unknown stopped operation remains visible after its last error log has
    # aged out. Sampling cannot clear that uncertainty or make it dispatchable.
    assert queue_ages(store)["model_prepare"] >= 40_000

    with store._connect() as connection:
        assert connection.execute("SELECT status FROM jobs").fetchone()["status"] == "stopped"
        connection.execute("UPDATE jobs SET due_at=clock_timestamp()+interval '40 seconds'")

    assert queue_ages(store) == {"model_prepare": 0, "model_infer": 0, "tool": 0}

    runtime.queue.cancel(context)

    with store._connect() as connection:
        connection.execute("UPDATE jobs SET due_at=clock_timestamp()-interval '40 seconds'")

    assert queue_ages(store)["model_prepare"] == 0
