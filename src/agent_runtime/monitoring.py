"""Process and queue observations; one bounded sampler per application process."""

import asyncio
import json
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import process_time
from typing import Literal

from agent_runtime.storage.connections import PrimaryConnections
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.telemetry import LOGGER, emit

Role = Literal["api", "coordinator", "tool"]


@dataclass(frozen=True)
class Resources:
    """Numeric observations only; unavailable current RSS remains unknown."""

    rss_bytes: int | None
    cpu_ms: int
    threads: int
    tasks: int
    pool_size: int
    pool_available: int
    pool_waiting: int
    pool_wait_ms: int
    pool_errors: int


def resources(connections: PrimaryConnections) -> Resources:
    """Sample the running event loop and the application's shared primary pool.

    Linux statm reports current resident pages, unlike ru_maxrss's lifetime peak.
    A non-Linux application can inject a sampler; otherwise RSS is null. The other
    counters remain available. CPU/pool waits are cumulative since process start;
    compare their deltas within one process, never across replacement replicas.
    """

    rss = None

    with suppress(OSError, ValueError, IndexError):
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        rss = resident_pages * os.sysconf("SC_PAGE_SIZE")

    pool = connections.pool.get_stats() if connections.pool is not None else {}
    return Resources(
        rss,
        round(process_time() * 1000),
        threading.active_count(),
        len(asyncio.all_tasks()),
        pool.get("pool_size", 0),
        pool.get("pool_available", 0),
        pool.get("requests_waiting", 0),
        pool.get("requests_wait_ms", 0),
        pool.get("requests_errors", 0),
    )


def queue_ages(store: PostgresStateStore) -> dict[str, int]:
    """Read the oldest due job in each active queue without changing its state.

    These are due ages, including work blocked by capacity, resources or an
    incompatible profile, plus active stopped work requiring operator recovery.
    They deliberately cover work that has never been claimed.
    A 30-second age requires operator inspection; it does not authorise replay.
    Future retries and inactive/cancelled conversations cannot produce an alarm.
    Each indexed scan returns one row and has a one-second database deadline.
    """

    result = {}

    with store._connect() as connection:
        connection.execute("SET LOCAL statement_timeout = '1s'")

        for queue in ("model_prepare", "model_infer", "tool"):
            row = connection.execute(
                "SELECT extract(epoch FROM (clock_timestamp()-j.due_at))*1000 AS age_ms "
                "FROM jobs j JOIN conversations c USING (tenant_id,conversation_id) "
                "JOIN runs r USING (tenant_id,conversation_id,run_id) "
                "WHERE j.operation=%s AND j.recovery IS NOT NULL "
                "AND (j.status IN ('ready','retry','reconcile','approval') OR j.status='stopped') "
                "AND j.due_at<=clock_timestamp() AND NOT c.deleted AND r.active "
                "AND c.active_run_id=j.run_id ORDER BY j.due_at,j.job_id LIMIT 1",
                (queue,),
            ).fetchone()
            result[queue] = round(row["age_ms"]) if row is not None else 0

    return result


class ProcessMonitor:
    """Emit passive health metadata without allocating anything per conversation.

    Application startup enters ProcessMonitor("api", connections) alongside its
    clients. A coordinator also passes its store for due-queue sampling. Every
    thirty seconds one task samples counters; only the short database probe uses
    an executor thread. Closing the context drains that probe before clients close.
    Failed probes emit a dropped-telemetry event and the next interval retries;
    they cannot trigger inference, touch activity or fail accepted work.

    Tests can inject sample=lambda: Resources(1024, 7, 2, 3, 4, 4, 0, 0, 0).
    This produces numeric process metadata without messages or credential fields.
    """

    def __init__(
        self,
        role: Role,
        connections: PrimaryConnections,
        *,
        store: PostgresStateStore | None = None,
        interval: float = 30,
        sample: Callable[[], Resources] | None = None,
    ) -> None:
        if role not in {"api", "coordinator", "tool"} or not 0 < interval <= 300:
            raise ValueError("invalid monitor configuration")

        self.role = role
        self.sample = sample or (lambda: resources(connections))
        self.store = store
        self.interval = interval
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.run())
        return self

    async def __aexit__(self, *args):
        self.stop.set()
        assert self.task is not None
        await self.task

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                record = {
                    "event": "runtime.resources",
                    "time": datetime.now(UTC).isoformat(),
                    "role": self.role,
                    "pid": os.getpid(),
                    **asdict(self.sample()),
                }
                LOGGER.info(json.dumps(record, separators=(",", ":")))

                if self.store is not None:
                    ages = await asyncio.to_thread(queue_ages, self.store)

                    for queue, age in ages.items():
                        emit("queue.sample", queue=queue, queue_wait_ms=age)
            except Exception:

                # Diagnostics have no authority to retry or abort application work.
                # Omit raw errors because client exceptions can contain credentials.
                emit("telemetry.dropped")

            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), self.interval)
