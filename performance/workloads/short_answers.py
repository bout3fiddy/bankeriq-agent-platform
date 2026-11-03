"""Fixed synthetic short-answer workload; runtime calls only, no infrastructure changes."""

import argparse
import asyncio
import json
import math
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx2


async def main(args):

    # Keep earlier evidence intact. Authentication and conversation creation are
    # setup; each timed attempt starts at durable run admission and ends at the
    # final history response. For concurrency=2 each worker has its own chat.
    if args.output.exists() or args.count < 1 or args.interval_seconds <= 0:
        raise ValueError("require a new output path and positive count/arrival interval")

    config = json.loads(args.config.read_text())
    token = json.loads(
        subprocess.check_output(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                "api://" + config["audience"],
                "-o",
                "json",
            ],
            text=True,
        )
    )["accessToken"]
    record = {
        "started_at": datetime.now(UTC).isoformat(),
        "concurrency": args.concurrency,
        "workload": "fixed-empty-history-ready",
        "poll_seconds": 0.05,
        "token_acquisition_included": False,
        "interval_seconds": args.interval_seconds,
        "attempts": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(record, indent=2) + "\n")

    async with httpx2.AsyncClient(
        base_url=config["url"], timeout=30, headers={"authorization": "Bearer " + token}
    ) as client:

        async def one(index):
            response = await client.post("/conversations")
            response.raise_for_status()
            conversation = response.json()["conversation_id"]
            run = str(uuid4())
            headers = {"x-conversation-id": conversation, "x-run-id": run}
            item = {
                "index": index,
                "conversation": conversation,
                "run": run,
                "status": "not_admitted",
                "correct": False,
                "admission_seconds": None,
                "turn_seconds": None,
                "started_at": datetime.now(UTC).isoformat(),
            }
            record["attempts"].append(item)
            save()
            started = time.monotonic()

            try:
                response = await client.post(
                    "/runs",
                    headers=headers,
                    json={"request_id": run, "text": "Reply with exactly READY."},
                )
                response.raise_for_status()
                item["admission_seconds"] = round(time.monotonic() - started, 6)
                deadline = time.monotonic() + 120

                while time.monotonic() < deadline:
                    response = await client.get("/status", headers=headers)
                    response.raise_for_status()
                    item["status"] = response.json()["status"]

                    if item["status"] in {"completed", "failed", "cancelled", "limit_reached"}:
                        break

                    await asyncio.sleep(0.05)

                response = await client.get("/history", headers=headers)
                response.raise_for_status()
                replies = [
                    e["payload"]["value"]["text"]
                    for e in response.json()["events"]
                    if e["run_id"] == run
                    and e["payload"]["type"] == "Message"
                    and e["payload"]["value"]["role"] == "assistant"
                ]
                item["correct"] = (
                    item["status"] == "completed"
                    and len(replies) == 1
                    and replies[0].strip() == "READY"
                )
                item["turn_seconds"] = round(time.monotonic() - started, 6)
            except Exception as error:
                item["error"] = type(error).__name__
                item["turn_seconds"] = round(time.monotonic() - started, 6)

            item["finished_at"] = datetime.now(UTC).isoformat()
            save()
            print(
                json.dumps({k: v for k, v in item.items() if k not in {"conversation", "run"}}),
                flush=True,
            )

        await one(0)
        queue = asyncio.Queue()

        for index in range(1, args.count + 1):
            queue.put_nowait(index)

        async def worker():
            while not queue.empty():

                # Fixed arrival spacing represents the declared pilot traffic envelope.
                # It is outside each measured turn and never changes runtime dispatch.
                started = time.monotonic()
                await one(queue.get_nowait())

                if not queue.empty():
                    await asyncio.sleep(
                        max(0, args.interval_seconds - (time.monotonic() - started))
                    )

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))

    samples = [a["turn_seconds"] for a in record["attempts"] if a["index"] > 0 and a["correct"]]
    record["ended_at"] = datetime.now(UTC).isoformat()
    record["summary"] = {
        "eligible": len(samples),
        "attempts": args.count,
        "p50_seconds": statistics.median(samples) if samples else None,
        "p95_seconds": sorted(samples)[math.ceil(0.95 * len(samples)) - 1] if samples else None,
    }
    save()
    print(json.dumps(record["summary"]), flush=True)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--config", type=Path, default=Path("scratch/live-client.json"))
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--concurrency", type=int, choices=[1, 2], required=True)
parser.add_argument("--count", type=int, default=100)
parser.add_argument("--interval-seconds", type=float, required=True)
asyncio.run(main(parser.parse_args()))
