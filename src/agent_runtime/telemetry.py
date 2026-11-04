"""Allowlisted lifecycle metadata for application-owned console log collection."""

import hashlib
import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

from agent_harness.runs.context import RunContext

Event = Literal[
    "runtime.ready",
    "runtime.drained",
    "admission.accepted",
    "admission.rejected",
    "session.activity",
    "cache.hit",
    "cache.miss",
    "cache.rebuilt",
    "worker.claimed",
    "worker.started",
    "worker.settled",
    "worker.retry",
    "worker.uncertain",
    "worker.lease_lost",
    "model.request",
    "memory.assembled",
    "memory.compacted",
    "model.capacity_wait",
    "runtime.span",
    "stream.opened",
    "stream.closed",
    "stream.failed",
    "stream.overflow",
    "stream.reset",
    "stream.final",
    "telemetry.dropped",
    "queue.sample",
]
LOGGER = logging.getLogger("bankeriq.lifecycle")
ACTIVE_RUN: ContextVar[RunContext | None] = ContextVar("active_run", default=None)
ACTIVE_JOB: ContextVar[str | None] = ContextVar("active_job", default=None)
ACTIVE_ATTEMPT: ContextVar[int | None] = ContextVar("active_attempt", default=None)


def configure() -> None:
    """Send JSON lines to stdout; Container Apps collects them into Log Analytics."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.handlers = [handler]
    LOGGER.propagate = False
    LOGGER.setLevel(logging.INFO)


def emit(
    event: Event,
    context: RunContext | None = None,
    *,
    queue: str | None = None,
    attempt: int | None = None,
    elapsed_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    status_code: int | None = None,
    token_limit: int | None = None,
    tokens_remaining: int | None = None,
    summary: bool | None = None,
    estimated_input_tokens: int | None = None,
    estimated_context_tokens: int | None = None,
    queue_wait_ms: int | None = None,
    queue_wait_upper_ms: int | None = None,
    stage: Literal["admission", "claim", "assembly", "settlement", "publication"] | None = None,
) -> None:
    """Emit correlation IDs and bounded counters without application payloads.

    Example: emit("worker.started", context, queue="tool", attempt=1) records
    an opaque conversation/run and the execution stage. No argument accepts text,
    tool inputs/results, credentials, exception messages or report locations.
    Unknown queue names collapse to "other" instead of entering diagnostic logs.
    Configure the sink only in application startup; core runtime has no logger.
    """

    record: dict[str, str | int] = {
        "event": event,
        "time": datetime.now(UTC).isoformat(),
    }

    if context is not None:
        record.update(
            tenant=hashlib.sha256(context.tenant_id.encode()).hexdigest()[:16],
            conversation=context.conversation_id,
            run=context.run_id,
        )

    if queue is not None:
        record["queue"] = queue if queue in {"model_prepare", "model_infer", "tool"} else "other"

    if job := ACTIVE_JOB.get():
        record["job"] = job

    if attempt is None:
        attempt = ACTIVE_ATTEMPT.get()

    if stage is not None:
        record["stage"] = stage

    if attempt is not None:
        record["attempt"] = attempt

    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms

    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("status_code", status_code),
        ("token_limit", token_limit),
        ("tokens_remaining", tokens_remaining),
        ("estimated_input_tokens", estimated_input_tokens),
        ("estimated_context_tokens", estimated_context_tokens),
        ("queue_wait_ms", queue_wait_ms),
        ("queue_wait_upper_ms", queue_wait_upper_ms),
    ):
        if value is not None:
            record[name] = value

    if summary is not None:
        record["purpose"] = "summary" if summary else "conversation"

    LOGGER.info(json.dumps(record, separators=(",", ":")))
