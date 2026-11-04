"""Stream non-sensitive operator conversations using the existing delegated Entra login."""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx2

from agent_platform.client.stream import RunPresentation
from agent_platform.client.transport import receive_run


async def run(args) -> None:
    config = json.loads(args.config.read_text())

    async def headers():
        raw = await asyncio.to_thread(
            subprocess.check_output,
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
        return {
            "authorization": "Bearer " + json.loads(raw)["accessToken"],
            "x-conversation-id": config.get("conversation", ""),
            "x-run-id": config.get("run", ""),
        }

    def save():
        args.config.write_text(json.dumps(config, indent=2) + "\n")
        args.config.chmod(0o600)

    async with httpx2.AsyncClient(base_url=config["url"], timeout=30) as client:
        authentication = await headers()

        if args.action == "new":
            response = await client.post("/conversations", headers=authentication)
            response.raise_for_status()
            config.update(conversation=response.json()["conversation_id"], run=str(uuid4()))
            save()
            print(json.dumps({"conversation": config["conversation"]}))
            return

        if not config.get("conversation"):
            raise ValueError("create a conversation first")

        started = monotonic()
        admission_seconds = None

        if args.action == "send":
            if not args.text:
                raise ValueError("send requires non-sensitive text")

            config["run"] = str(uuid4())
            save()
            authentication["x-run-id"] = config["run"]
            started = monotonic()
            response = await client.post(
                "/runs",
                headers=authentication,
                json={"request_id": config["run"], "text": args.text},
            )
            response.raise_for_status()
            admission_seconds = monotonic() - started

        displayed = ""
        first_text = None

        def update(state):
            nonlocal displayed, first_text

            # Model text cannot inject terminal escape/control sequences. A
            # changed generation is visibly replaced, then final text wins.
            visible = "".join(c for c in state.text if c.isprintable() or c in "\n\t")

            if visible and first_text is None:
                first_text = monotonic() - started

            if visible.startswith(displayed):
                print(visible[len(displayed) :], end="", flush=True)
            elif visible != displayed:
                print("\n[answer updated]\n" + visible, end="", flush=True)

            displayed = visible

        # Reuse the token acquired before timing on the first subscription;
        # subsequent reconnects refresh it through the credential callback.
        first_subscription = True

        async def stream_headers():
            nonlocal first_subscription

            if first_subscription:
                first_subscription = False
                return dict(authentication)

            return await headers()

        result = await receive_run(
            client,
            RunPresentation(config["run"]),
            headers=stream_headers,
            on_update=update,
            timeout=args.wait_seconds,
        )
        assert result.final is not None
        print(
            "\n"
            + json.dumps(
                {
                    "run": config["run"],
                    "status": result.final["status"],
                    "admission_seconds": admission_seconds,
                    "first_text_seconds": first_text,
                    "turn_seconds": monotonic() - started,
                    "progress_gaps": result.gaps,
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("scratch/live-client.json"))
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("action", choices=["new", "send", "resume"])
    parser.add_argument("--text")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
