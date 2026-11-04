"""Scoped numeric provider observations for operational and validation accounting."""

import asyncio
from contextlib import suppress
from uuid import uuid4

from agent_harness.runs.context import RunContext
from agent_runtime.providers.foundry.metrics import ModelObservation
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.telemetry import emit


class UsageWriter:
    """Bound the observation backlog without blocking model transport or settlement.

    Startup owns this async context and offers immutable numeric observations.
    One writer uses a thread only during active SQL work. Overflow/failure emits
    a fixed dropped-telemetry event; validation must treat missing observations
    as a gap. The model call is never retried because a monitoring sink failed.
    """

    def __init__(self, store: PostgresStateStore) -> None:
        self.store = store
        self.pending: asyncio.Queue[tuple[RunContext, ModelObservation]] = asyncio.Queue(128)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._write())
        return self

    async def __aexit__(self, *args):
        with suppress(TimeoutError):
            await asyncio.wait_for(self.pending.join(), 10)

        if not self.pending.empty():
            emit("telemetry.dropped")

        assert self.task is not None
        self.task.cancel()

        with suppress(asyncio.CancelledError):
            await self.task

    def offer(self, context: RunContext, record: ModelObservation) -> None:
        try:
            self.pending.put_nowait((context, record))
        except asyncio.QueueFull:
            emit("telemetry.dropped", context)

    async def _write(self) -> None:
        while True:
            context, record = await self.pending.get()

            try:
                await asyncio.to_thread(record_usage, self.store, context, record)
            except Exception:
                emit("telemetry.dropped", context)
            finally:
                self.pending.task_done()


def record_usage(store: PostgresStateStore, context: RunContext, record: ModelObservation) -> None:
    """Persist an observation without claiming complete provider billing coverage.

    A socket interruption may omit usage, which remains NULL and counts as unknown.
    A process killed before its observer runs can leave no observation at all; the
    validation runner must reconcile completed model steps and summary attempts.
    Recording failure never retries inference. Stable observation IDs make database
    retries idempotent, including an unknown commit acknowledgement.
    """

    identifier = str(uuid4())

    def transaction(connection):
        store._generation(connection)
        connection.execute(
            "INSERT INTO model_usage (observation_id,tenant_id,conversation_id,run_id,purpose,"
            "elapsed_ms,status_code,input_tokens,output_tokens) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (observation_id) DO NOTHING",
            (
                identifier,
                *store._key(context),
                "summary" if record.summary else "conversation",
                record.elapsed_ms,
                record.status_code,
                record.input_tokens,
                record.output_tokens,
            ),
        )

    store._retry(transaction)
