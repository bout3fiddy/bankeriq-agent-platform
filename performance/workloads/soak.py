"""Run the authorised two-hour synthetic conversation soak using a delegated identity."""

import argparse
import asyncio
import json
import re
import subprocess
from pathlib import Path

import httpx2

from tests.soak.runner import SoakBudget, SoakPlan, run_soak
from tests.soak.transport import CliTokens, HttpSoakClient


async def main(args: argparse.Namespace) -> int:
    config = json.loads(args.config.read_text())
    dialogue = tuple(" ".join(parts) for parts in json.loads(args.dialogue.read_text()))
    tokens = CliTokens(config["audience"], args.tenant, args.user, args.azure_config_dir)
    plan = SoakPlan(
        max_turns=args.max_turns,
        budget=SoakBudget(
            model_calls=args.budget_calls,
            input_tokens=args.budget_input_tokens,
            output_tokens=args.budget_output_tokens,
            reserve_calls=args.reserve_calls,
        ),
    )

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_digest):
        raise ValueError("require the inspected deployed image digest")

    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    source_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True))

    # A caller supplies a fresh ledger and binds the expected delegated identity.
    # The selected group-12 run uses the current login. The base config is read-only:
    # existing user conversations and login state survive.
    async with httpx2.AsyncClient(base_url=config["url"], timeout=30) as client:
        record = await run_soak(
            HttpSoakClient(client, tokens),
            plan,
            dialogue,
            args.output,
            stopped=lambda: args.stop.exists(),
            source_commit=source_commit,
            image_digest=args.image_digest,
            source_dirty=source_dirty,
        )

    print(
        json.dumps(
            {
                "status": record["status"],
                "checks": record["checks"],
                "elapsed_seconds": record["elapsed_seconds"],
                "token_refreshes": tokens.refreshes,
                "usage": record["usage"],
                "reserved_usage": record["reserved_usage"],
            }
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--azure-config-dir", type=Path)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--budget-calls", type=int, required=True)
    parser.add_argument("--budget-input-tokens", type=int, required=True)
    parser.add_argument("--budget-output-tokens", type=int, required=True)
    parser.add_argument("--reserve-calls", type=int, default=100)
    parser.add_argument("--dialogue", type=Path, default=Path("tests/soak/dialogue.json"))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
