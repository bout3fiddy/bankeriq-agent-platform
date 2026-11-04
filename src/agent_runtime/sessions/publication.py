"""Coalesced provisional text publication with primary ownership and policy checks."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from time import monotonic

from agent_harness.coordinator.model import ModelReply, ModelRequest, StreamingModelClient
from agent_harness.runs.context import RunContext
from agent_harness.storage.interface import StateConflictError
from agent_runtime.sessions.service import SessionService
from agent_runtime.telemetry import emit
from agent_runtime.workers.queue import Lease, PostgresQueue


class TextPublication:
    """Publish only text explicitly permitted by application-owned policy.

    For example, an operator-only synthetic composition supplies allow(context)
    from its live allowlist. A deployment requiring whole-answer review leaves
    this adapter unwired. No model flag or HTTP input can enable publication.
    At most one Redis write runs per inference, and new deltas replace the pending
    snapshot while that write is slow. Settlement never waits for a reader.
    """

    def __init__(
        self, sessions: SessionService, queue: PostgresQueue, *, allow: Callable[[RunContext], bool]
    ) -> None:
        self.sessions = sessions
        self.queue = queue
        self.allow = allow

    async def complete(
        self, model: StreamingModelClient, request: ModelRequest, lease: Lease
    ) -> ModelReply:
        text = ""
        sequence = 0
        kind = "text"
        changed = asyncio.Event()

        async def on_text(delta: str) -> None:
            nonlocal text, kind

            if kind == "reset":
                return

            text += delta

            if len(text.encode()) > 32768:
                text, kind = "", "reset"

            changed.set()

        async def publish() -> None:
            nonlocal sequence

            while True:
                await changed.wait()
                changed.clear()
                sequence += 1

                # Snapshot scalar values before dispatching blocking adapters.
                # Database/cache outages drop provisional progress only; the
                # captured model result still follows ordinary durable settlement.
                with suppress(Exception):
                    await asyncio.to_thread(self.write, lease, sequence, text, kind)

                await asyncio.sleep(0.05)

        producer = asyncio.create_task(publish())

        try:
            reply = await model.stream(request, on_text)

            if reply.answer is None:
                kind = "reset"

            return reply
        except BaseException:
            kind = "reset"
            raise
        finally:
            producer.cancel()

            with suppress(asyncio.CancelledError):
                await producer

            if kind == "reset":
                with suppress(Exception):
                    await asyncio.to_thread(self.write, lease, sequence + 1, "", "reset")

    def write(self, lease: Lease, sequence: int, text: str, kind: str = "text") -> None:
        """Check a live owner before Redis; readers recheck after the transaction ends.

        A cancellation can commit between this check and the Redis write. Its
        newer run revision/lease token makes that snapshot ineligible for delivery.
        These small metadata reads never load or copy the conversation transcript.
        """

        started = monotonic()

        if not self.allow(lease.context):
            raise StateConflictError("provisional publication denied")

        if sequence < 1 or len(text.encode()) > 32768 or kind not in {"text", "reset"}:
            raise ValueError("invalid provisional text")

        with self.sessions.store._connect() as connection:
            head = self.sessions.store._head(connection, lease.context)
            self.queue._fence(connection, lease)
            row = connection.execute(
                "SELECT revision FROM runs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s",
                self.sessions.store._key(lease.context),
            ).fetchone()

            if row is None or row["revision"] != lease.checkpoint.run.revision:
                raise StateConflictError("run advanced beyond text owner")

        self.sessions.cache.text(
            head,
            lease.context.run_id,
            {
                "run_id": lease.context.run_id,
                "step": str(lease.state.arguments["step"]),
                "attempt": str(lease.token),
                "job_id": lease.job_id,
                "sequence": str(sequence),
                "text": text,
                "kind": kind,
                "provisional": True,
            },
        )
        emit(
            "runtime.span",
            lease.context,
            stage="publication",
            elapsed_ms=round((monotonic() - started) * 1000),
        )
