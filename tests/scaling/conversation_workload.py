"""Scheduled arrivals and exact client boundaries for a bounded streamed-answer cohort."""

import asyncio
import json
import math
import statistics
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, time
from typing import Any
from uuid import uuid4

import httpx2

from agent_platform.client.stream import RunPresentation
from agent_platform.client.transport import receive_run
from tests.scaling.clocks import offset_interval

# Fixed reviewed text isolates output-length cost from open-ended answer quality.
# Memory fidelity uses a separate semantic suite; matching this paragraph alone
# establishes transport correctness and the declared short-answer token length.
PARAGRAPH = (
    "A bank statement lists the money entering and leaving an account during a defined period. "
    "It normally shows opening and closing balances, transaction dates, descriptions, and amounts. "
    "Reviewing it helps identify unexpected charges, compare spending with a budget, and check "
    "whether expected payments arrived. Keep the statement secure because it contains financial "
    "information about the account holder."
)


def distribution(values):
    return {
        "count": len(values),
        "p50": statistics.median(values) if values else None,
        "p95": sorted(values)[math.ceil(0.95 * len(values)) - 1] if values else None,
    }


async def cohort(
    client: httpx2.AsyncClient,
    *,
    headers: Callable[[], Awaitable[dict[str, str]]],
    output: Path,
    sessions: int,
    count: int,
    interval: float,
    expected: str = PARAGRAPH,
    wait_seconds: float = 30,
    collect_usage: bool = True,
    stopped: Callable[[], bool] = lambda: False,
    on_result: Callable[[dict[str, Any]], None] = lambda row: None,
    worker_budget: int | None = None,
) -> dict[str, Any]:
    """Run fixed arrivals without hiding backlog behind completed responses.

    A batch of five at t=0,15,30 seconds offers twenty turns/minute. Each
    attempt gets a fresh conversation to keep prompt/history sizes comparable.
    If an earlier batch is still active, the next scheduled batch remains in
    the ledger as client_backlog rather than inflating the active-session tier.
    Accepted timeouts retain their run IDs; no automatic resubmission occurs.
    """

    if sessions not in {1, 2, 5, 10} or not 1 <= count <= 1000 or interval <= 0:
        raise ValueError("invalid finite cohort")

    record: dict[str, Any] = {
        "sessions": sessions,
        "count": count,
        "batch_interval_seconds": interval,
        "started_at": datetime.now(UTC).isoformat(),
        "token_acquisition_included": False,
        "worker_budget": worker_budget,
        "attempts": [],
        "peak_active": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("x"):
        output.chmod(0o600)

    def save():
        output.write_text(json.dumps(record, indent=2) + "\n")

    # Create all scopes before the timed arrival schedule. Every admission has
    # a stable ID persisted before POST, including warmup and rejected attempts.
    for index in range(count + 1):
        response = await client.post("/conversations", headers=await headers())
        response.raise_for_status()
        record["attempts"].append(
            {
                "index": index,
                "warmup": index == 0,
                "run": str(uuid4()),
                "conversation": response.json()["conversation_id"],
                "status": "scheduled",
            }
        )

    save()
    active = 0

    async def recover(row):
        async def recovery_headers():
            return {
                **await headers(),
                "x-conversation-id": row["conversation"],
                "x-run-id": row["run"],
            }

        try:

            # A subscription rejection does not reject the already committed run.
            # Keep its client slot occupied until bounded read-only recovery ends.
            # Otherwise ten rejected readers could release slots for another ten
            # admissions while their original model calls still consume budget.
            async with asyncio.timeout(wait_seconds):
                while True:
                    try:
                        recovered = await receive_run(
                            client,
                            RunPresentation(row["run"]),
                            headers=recovery_headers,
                            on_update=lambda state: None,
                            timeout=wait_seconds,
                        )
                        break
                    except httpx2.HTTPStatusError as error:
                        if error.response.status_code != 429:
                            raise

                        await asyncio.sleep(0.25)

            assert recovered.final is not None
            row["recovery"] = {
                "status": recovered.final["status"],
                "correct": recovered.text.strip() == expected,
            }

            if collect_usage:
                response = await client.get("/inspect", headers=await recovery_headers())
                response.raise_for_status()
                row["usage"] = response.json()["usage"]
        except Exception as error:
            row["recovery"] = {"error": type(error).__name__}

        save()

    async def one(row):
        nonlocal active

        try:
            authentication = await headers()
            authentication.update(
                {"x-conversation-id": row["conversation"], "x-run-id": row["run"]}
            )

            # Bound cross-host clock uncertainty with a primary timestamp inside
            # an authenticated round trip. Preserve the full interval.
            sent = time()
            response = await client.get("/inspect", headers=authentication)
            received = time()
            response.raise_for_status()
            observation = response.json()
            offsets = offset_interval(observation, sent=sent, received=received)
            row["database_clock_offset_seconds"] = list(offsets)
            row["database_clock_server_timing"] = observation.get("database_clock_timing")
        except Exception as error:
            row.update(status="setup_failed", error=type(error).__name__)
            save()
            return

        active += 1
        record["peak_active"] = max(active, record["peak_active"])
        started = monotonic()
        row.update(
            status="not_admitted", started_at=datetime.now(UTC).isoformat(), first_text_seconds=None
        )
        save()

        async def scoped():
            return dict(authentication)

        def update(state):
            if state.text and row["first_text_seconds"] is None:
                row["first_text_seconds"] = monotonic() - started

        try:
            response = await client.post(
                "/runs",
                headers=authentication,
                json={
                    "request_id": row["run"],
                    "text": "Reply with exactly this text: " + expected,
                    **({"worker_budget": worker_budget} if worker_budget is not None else {}),
                },
            )
            row["admission_http_status"] = response.status_code
            row["admission_seconds"] = monotonic() - started
            response.raise_for_status()
            row["status"] = "accepted"
            state = await receive_run(
                client,
                RunPresentation(row["run"]),
                headers=scoped,
                on_update=update,
                timeout=wait_seconds,
            )
            final_time = time()
            row["turn_seconds"] = monotonic() - started
            assert state.final is not None
            row.update(
                status=state.final["status"],
                correct=state.text.strip() == expected,
                gaps=state.gaps,
            )
            committed = state.final.get("committed_at")
            written = state.final.get("terminal_written_at")

            for name, timestamp in (("commit", committed), ("terminal_write", written)):
                if timestamp is not None:
                    delta = final_time - datetime.fromisoformat(timestamp).timestamp()
                    row[name + "_delivery_seconds"] = [delta + offset for offset in offsets]

            # Numeric observations can arrive just after final settlement. Wait
            # outside turn timing; an absent/partial observation stays a gap.
            for _ in range(50 if collect_usage else 0):
                response = await client.get("/inspect", headers=authentication)
                response.raise_for_status()
                usage = response.json()["usage"]
                row["usage"] = usage

                if usage["model_calls"] and not usage["unknown_usage"]:
                    break

                await asyncio.sleep(0.1)
        except TimeoutError:
            row["status"] = "pending_timeout"
        except httpx2.HTTPStatusError as error:
            row["error"] = "http_failure"
            row["http_status"] = error.response.status_code
        except httpx2.TransportError, ConnectionError:
            row["error"] = "transport_failure"
        except Exception as error:
            row["error"] = type(error).__name__
        finally:
            if row.get("admission_http_status") == 202 and row.get("http_status") == 429:
                await recover(row)

            active -= 1
            row["elapsed_seconds"] = monotonic() - started
            on_result(row)
            save()

    await one(record["attempts"][0])
    started = monotonic()
    pending: set[asyncio.Task[None]] = set()

    for offset in range(0, count, sessions):
        scheduled = offset // sessions * interval
        await asyncio.sleep(max(0, started + scheduled - monotonic()))
        pending = {task for task in pending if not task.done()}

        for row in record["attempts"][offset + 1 : offset + sessions + 1]:
            row["scheduled_seconds"] = scheduled
            row["arrival_lag_seconds"] = monotonic() - started - scheduled

            if stopped():
                row["status"] = "campaign_stopped"
            elif len(pending) >= sessions:
                row["status"] = "client_backlog"
            else:
                pending.add(asyncio.create_task(one(row)))

        save()

    if pending:
        await asyncio.gather(*pending)

    # Retry remaining accepted timeouts after the burst drains, using the same
    # run IDs. Subscription rejections were already reconciled before releasing
    # their slots. Keep recovery timing outside the original latency cohort.
    for row in record["attempts"]:
        if row["status"] not in {"accepted", "pending_timeout"} or row.get("recovery"):
            continue

        await recover(row)

    measured = record["attempts"][1:]
    correct = [row for row in measured if row.get("correct") and row["status"] == "completed"]
    record["elapsed_seconds"] = monotonic() - started
    record["offered_per_minute"] = 60 * sessions / interval
    record["achieved_per_minute"] = 60 * len(correct) / record["elapsed_seconds"]
    record["summary"] = {
        key: distribution([row[key] for row in correct if row.get(key) is not None])
        for key in ("admission_seconds", "first_text_seconds", "turn_seconds")
    }
    record["failed_or_unfinished"] = count - len(correct)
    save()
    return record
