"""Finite paired arrivals measure ordinary answers while another chat compacts."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from uuid import uuid4

from tests.scaling.conversation_workload import PARAGRAPH, distribution
from tests.soak.runner import Session, SoakClient

# Each accepted pilot run can consume at most 100 worker attempts. Reserve the
# full provider envelope before admission, then release unused headroom only
# after terminal state and complete numeric usage agree. Two overlapping turns
# can therefore reserve 200 calls; losing one response cannot release its cost.
RESERVE = {"model_calls": 100, "input_tokens": 819200, "output_tokens": 102400}


async def run_mixed(
    client: SoakClient,
    output: Path,
    *,
    limits: dict[str, int],
    memory_text: str,
    batches: int = 200,
    interval: float = 12,
    stopped: Callable[[], bool] = lambda: False,
):
    if not 1 <= batches <= 250 or interval <= 0 or not 1 <= len(memory_text.encode()) <= 8192:
        raise ValueError("invalid bounded mixed workload")

    if set(limits) != set(RESERVE) or any(limits[key] < RESERVE[key] for key in RESERVE):
        raise ValueError("budget must cover at least one complete pilot run")

    record = {
        "profile": "paired ordinary answers and repeated automatic compaction",
        "batches": batches,
        "batch_interval_seconds": interval,
        "offered_turns_per_minute": 120 / interval,
        "limits": limits,
        "usage": dict.fromkeys(RESERVE, 0),
        "reservations": {},
        "attempts": [],
        "stopped": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("x"):
        output.chmod(0o600)

    def save():
        output.write_text(json.dumps(record, indent=2) + "\n")

    memory = await client.create()
    ordinary = [await client.create() for _ in range(batches)]
    previous = {}

    async def one(row, session, text, expected):
        scope = Session(session.conversation, row["run"])
        before = previous.get(session.conversation)

        if before is None:
            try:
                before = await client.inspect(scope)
            except Exception as error:
                row.update(status="setup_failed", error=type(error).__name__)
                record["stopped"] = True
                save()
                return

        baseline = before.get("usage")

        if (
            baseline is None
            or before["active_run"] is not None
            or baseline["unknown_usage"]
            or baseline["model_calls"] < baseline["expected_calls_lower_bound"]
        ):
            row["status"] = "setup_unresolved"
            record["stopped"] = True
            save()
            return

        # Reserve synchronously before the first admission await. Concurrent lanes
        # see each other's allocation, including a previous uncertain admission.
        reserved = {
            key: sum(value[key] for value in record["reservations"].values()) for key in RESERVE
        }

        if (
            record["stopped"]
            or stopped()
            or any(
                record["usage"][key] + reserved[key] + RESERVE[key] > limits[key] for key in RESERVE
            )
        ):
            row["status"] = "campaign_stopped"
            record["stopped"] = True
            save()
            return

        record["reservations"][row["run"]] = dict(RESERVE)
        row["status"] = "starting"
        save()

        try:
            row.update(await client.turn(scope, text, expected))
            terminal = row["status"] in {"completed", "failed", "cancelled", "limit_reached"}

            # /inspect is passive and conversation-scoped. A lane remains occupied
            # through reconciliation, so its next turn cannot contaminate the usage
            # delta. Timed completion excludes these reads; backlog retains that limit.
            for _ in range(20):
                after = await client.inspect(scope)
                usage = after.get("usage")
                known = (
                    terminal
                    and after["active_run"] is None
                    and usage is not None
                    and usage["unknown_usage"] == 0
                    and usage["model_calls"] >= usage["expected_calls_lower_bound"]
                    and usage["model_calls"] > before["usage"]["model_calls"]
                )

                if known:
                    break

                await asyncio.sleep(0.25)
            else:
                row["usage_unresolved"] = True
                record["stopped"] = True
                return

            assert usage is not None
            delta = {key: usage[key] - before["usage"][key] for key in RESERVE}

            if any(not 0 <= delta[key] <= RESERVE[key] for key in RESERVE):
                row["usage_unresolved"] = True
                record["stopped"] = True
                return

            row["usage"] = delta

            if row["lane"] == "ordinary" and (
                delta["model_calls"] != 1
                or delta["input_tokens"] > 1800
                or not 50 <= delta["output_tokens"] <= 100
            ):
                row["workload_invalid"] = True
                record["stopped"] = True

            if min(before["summary_versions"], after["summary_versions"]) < 0:
                row["summary_publications"] = None
                record["stopped"] = True
            else:
                row["summary_publications"] = after["summary_versions"] - before["summary_versions"]

            previous[session.conversation] = after
            record["reservations"].pop(row["run"])

            for key in RESERVE:
                record["usage"][key] += delta[key]

            if row["status"] != "completed" or row.get("correct") is not True:
                record["stopped"] = True
        except Exception as error:
            row["error"] = type(error).__name__
            record["stopped"] = True
        finally:
            save()

    # Five real preparation turns establish retained history outside the timed
    # cohorts. They consume the same finite campaign allocation. Every later
    # memory turn is classified by actual publication count; zero or multiple
    # summaries cannot silently become a one-compaction latency observation.
    for index in range(5):
        row = {
            "index": index,
            "lane": "memory",
            "warmup": True,
            "run": str(uuid4()),
            "conversation": memory.conversation,
        }
        record["attempts"].append(row)
        await one(row, memory, memory_text, "READY")

        if record["stopped"]:
            break

    started = monotonic()
    pending = {}

    for index in range(batches):
        if not record["stopped"] and not stopped():
            await asyncio.sleep(max(0, started + index * interval - monotonic()))

        for lane, session, text, expected in (
            ("ordinary", ordinary[index], "Reply with exactly this text: " + PARAGRAPH, PARAGRAPH),
            ("memory", memory, memory_text, "READY"),
        ):
            row = {
                "index": index,
                "lane": lane,
                "warmup": False,
                "run": str(uuid4()),
                "conversation": session.conversation,
                "scheduled_seconds": index * interval,
                "arrival_lag_seconds": monotonic() - started - index * interval,
            }
            record["attempts"].append(row)

            if record["stopped"] or stopped():
                row["status"] = "campaign_stopped"
            elif lane in pending and not pending[lane].done():
                row["status"] = "client_backlog"
            else:
                pending[lane] = asyncio.create_task(one(row, session, text, expected))

        save()

    await asyncio.gather(*pending.values())
    record["elapsed_seconds"] = monotonic() - started
    record["summary"] = {}

    for category in (
        "ordinary",
        "memory_without_compaction",
        "memory_one_compaction",
        "memory_other",
    ):

        def selected(row, category=category):
            if (
                row["warmup"]
                or row.get("status") != "completed"
                or not row.get("correct")
                or row.get("workload_invalid")
            ):
                return False

            publications = row.get("summary_publications")
            group = (
                "ordinary"
                if row["lane"] == "ordinary"
                else "memory_without_compaction"
                if publications == 0
                else "memory_one_compaction"
                if publications == 1
                else "memory_other"
            )
            return group == category

        rows = [row for row in record["attempts"] if selected(row)]
        record["summary"][category] = {
            "count": len(rows),
            **{
                key: distribution([row[key] for row in rows if row.get(key) is not None])
                for key in ("admission_seconds", "first_text_seconds", "turn_seconds")
            },
        }

    save()
    return record
