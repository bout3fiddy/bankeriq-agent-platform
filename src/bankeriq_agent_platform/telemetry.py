"""Allowlisted lifecycle metadata for application-owned console log collection."""

import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Literal

from agent_runtime.runs.context import RunContext

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
]
LOGGER = logging.getLogger("bankeriq.lifecycle")


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

    if attempt is not None:
        record["attempt"] = attempt

    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms

    LOGGER.info(json.dumps(record, separators=(",", ":")))
