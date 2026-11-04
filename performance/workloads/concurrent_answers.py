"""Finite hosted streaming cohorts using the same arrival ledger as the local workload."""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

import httpx2

from tests.scaling.conversation_workload import PARAGRAPH, cohort
from tests.soak.transport import CliTokens


async def main(args):
    if args.worker_budget is not None and not 1 <= args.worker_budget <= 100:
        raise ValueError("worker budget must be between one and 100 attempts")

    config = json.loads(args.config.read_text())
    tokens = CliTokens(config["audience"], args.tenant, args.user, None)
    stopped = False
    anomalies = []
    observed = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}

    async def headers():
        return {"authorization": "Bearer " + await tokens.token()}

    def check(row):
        nonlocal stopped
        usage = row.get("usage")

        # The fixed paragraph should require one bounded model call. Stop later
        # arrivals on unknown/missing usage, retries or a changed input envelope.
        # An uncertain POST also stops arrivals: missing acknowledgement is not
        # proof of rejection. Accepted work retains its IDs for bounded recovery.
        # This observes usage; an in-flight provider call is not a billing meter.
        if usage is None:
            if row.get("admission_http_status") == 202 or (
                row.get("status") == "not_admitted"
                and row.get("admission_http_status") is None
                and row.get("error")
            ):
                anomalies.append({"index": row["index"], "reason": "usage_missing"})
                stopped = True
            return

        for key in observed:
            observed[key] += usage[key]

        if (
            usage["unknown_usage"]
            or usage["model_calls"] != 1
            or usage["input_tokens"] > 1800
            or usage["output_tokens"] > args.max_output_tokens
        ):
            anomalies.append({"index": row["index"], "reason": "workload_or_usage_changed"})
            stopped = True

        if (
            observed["input_tokens"] > args.budget_input_tokens
            or observed["output_tokens"] > args.budget_output_tokens
        ):
            anomalies.append({"index": row["index"], "reason": "usage_limit"})
            stopped = True

    # A changed response or missing usage stops later arrivals. At most sessions
    # runs are already in flight then, so reserve their entire reduced worker
    # budgets in addition to every expected ordinary response. This covers their
    # bounded retries, summaries or tool follow-ups even if a client times out.
    extra_calls = args.sessions * args.worker_budget if args.worker_budget is not None else 0

    if args.budget_input_tokens < (args.count + 1) * 1800 + extra_calls * 8192:
        raise ValueError("reserve the declared input envelope before starting this cohort")

    if not 1 <= args.max_output_tokens <= 100:
        raise ValueError("output envelope must be between one and 100 tokens")

    if args.budget_output_tokens < (args.count + 1) * args.max_output_tokens + extra_calls * 1024:
        raise ValueError("reserve the declared output envelope before starting this cohort")

    await tokens.token()

    async with httpx2.AsyncClient(base_url=config["url"], timeout=30) as client:
        record = await cohort(
            client,
            headers=headers,
            output=args.output,
            sessions=args.sessions,
            count=args.count,
            interval=args.sessions * 3,
            expected="READY" if args.control else PARAGRAPH,
            stopped=lambda: stopped or args.stop.exists(),
            on_result=check,
            worker_budget=args.worker_budget,
        )

    record.update(
        workload="two-token-control" if args.control else "fixed-paragraph-streaming",
        image_digest=args.image_digest,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        source_dirty=bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
        anomalies=anomalies,
        limits={
            "input_tokens": args.budget_input_tokens,
            "output_tokens": args.budget_output_tokens,
            "planned_model_calls": args.count + 1,
            "maximum_model_calls_with_inflight_reservations": args.count + 1 + extra_calls,
            "output_tokens_per_call": args.max_output_tokens,
        },
        usage={
            key: sum(row.get("usage", {}).get(key, 0) for row in record["attempts"])
            for key in observed
        },
    )
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("summary", "usage", "anomalies")}), flush=True)
    return 1 if anomalies else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--sessions", type=int, choices=[1, 2, 5, 10], required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--budget-input-tokens", type=int, required=True)
    parser.add_argument("--budget-output-tokens", type=int, required=True)
    parser.add_argument("--max-output-tokens", type=int, default=100)
    parser.add_argument("--worker-budget", type=int)
    parser.add_argument("--control", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
