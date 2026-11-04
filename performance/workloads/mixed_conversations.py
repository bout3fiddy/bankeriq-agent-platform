"""Explicitly authorised mixed memory/ordinary traffic with full attempt reservations."""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from time import monotonic

import httpx2

from agent_platform.client.stream import RunPresentation
from agent_platform.client.transport import receive_run
from tests.mixed.runner import run_mixed
from tests.soak.transport import CliTokens, HttpSoakClient


class StreamingClient(HttpSoakClient):
    async def turn(self, session, text, expected):
        authentication = await self.headers(session)
        started = monotonic()
        row = {"status": "not_admitted", "correct": False, "first_text_seconds": None}

        async def headers():
            return dict(authentication)

        def update(state):
            if state.text and row["first_text_seconds"] is None:
                row["first_text_seconds"] = monotonic() - started

        try:
            response = await self.client.post(
                "/runs", headers=authentication, json={"request_id": session.run, "text": text}
            )
            row["admission_http_status"] = response.status_code
            row["admission_seconds"] = monotonic() - started
            response.raise_for_status()
            row["status"] = "accepted"
            state = await receive_run(
                self.client,
                RunPresentation(session.run),
                headers=headers,
                on_update=update,
                timeout=30,
            )
            assert state.final is not None
            row.update(
                status=state.final["status"],
                correct=state.text.strip() == expected,
                gaps=state.gaps,
            )
        except TimeoutError:
            row["status"] = "pending_timeout"
        except httpx2.HTTPStatusError as error:
            row.update(error="http_failure", http_status=error.response.status_code)
        except httpx2.TransportError:
            row["error"] = "transport_failure"

        row["turn_seconds"] = monotonic() - started
        return row


async def main(args):
    config = json.loads(args.config.read_text())
    tokens = CliTokens(config["audience"], args.tenant, args.user, None)
    await tokens.token()
    text = " ".join(json.loads(Path("tests/soak/dialogue.json").read_text())[0])

    async with httpx2.AsyncClient(base_url=config["url"], timeout=30) as client:
        record = await run_mixed(
            StreamingClient(client, tokens),
            args.output,
            limits={
                "model_calls": args.budget_calls,
                "input_tokens": args.budget_input_tokens,
                "output_tokens": args.budget_output_tokens,
            },
            memory_text=text,
            batches=args.batches,
            stopped=lambda: args.stop.exists(),
        )

    record.update(
        image_digest=args.image_digest,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        source_dirty=bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
        token_acquisition_included=False,
    )
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("summary", "usage", "reservations", "stopped")}))
    return int(
        record["stopped"]
        or any(
            row.get("status") != "completed" or row.get("correct") is not True
            for row in record["attempts"]
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--batches", type=int, default=200)
    parser.add_argument("--budget-calls", type=int, required=True)
    parser.add_argument("--budget-input-tokens", type=int, required=True)
    parser.add_argument("--budget-output-tokens", type=int, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
