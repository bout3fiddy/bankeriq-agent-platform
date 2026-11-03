"""Operator CLI for persistent non-sensitive conversations and passive inspection."""

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("scratch/live-client.json"))
    parser.add_argument("action", choices=["new", "send", "read", "inspect", "status", "activity"])
    parser.add_argument("--text")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())

    # Local configuration holds endpoints and opaque conversation IDs only. CLI
    # login provides a fresh token in memory for each invocation, including a
    # return after hours away. No token is written to disk or passed in argv.
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

    with httpx2.Client(
        base_url=config["url"], timeout=30, headers={"authorization": "Bearer " + token}
    ) as client:
        if args.action == "new":
            response = client.post("/conversations")
            response.raise_for_status()
            config["conversation"] = response.json()["conversation_id"]
            config["run"] = str(uuid4())
            save(args.config, config)
            print(json.dumps({"conversation": config["conversation"]}))
            return

        if "conversation" not in config:
            raise ValueError("create a conversation with the new action first")

        client.headers["x-conversation-id"] = config["conversation"]
        client.headers["x-run-id"] = config["run"]

        if args.action == "send":
            if not args.text:
                raise ValueError("send requires --text with non-sensitive test content")

            # Persist the new request identity before admission so an interrupted
            # client can inspect that same run without silently resubmitting it.
            config["run"] = str(uuid4())
            save(args.config, config)
            client.headers["x-run-id"] = config["run"]
            started = time.monotonic()
            response = client.post("/runs", json={"request_id": config["run"], "text": args.text})
            response.raise_for_status()
            admitted = time.monotonic() - started
            status = {}
            deadline = time.monotonic() + 180

            while time.monotonic() < deadline:
                response = client.get("/status")
                response.raise_for_status()
                status = response.json()

                if status["status"] in {"completed", "failed", "cancelled"}:
                    break

                time.sleep(0.5)

            history = client.get("/history")
            history.raise_for_status()
            replies = [
                event["payload"]["value"]["text"]
                for event in history.json()["events"]
                if event["run_id"] == config["run"]
                and event["payload"]["type"] == "Message"
                and event["payload"]["value"]["role"] == "assistant"
            ]
            print(
                json.dumps(
                    {
                        "conversation": config["conversation"],
                        "run": config["run"],
                        "status": status.get("status"),
                        "admission_seconds": round(admitted, 3),
                        "turn_seconds": round(time.monotonic() - started, 3),
                        "replies": replies,
                        "children": status.get("children"),
                        "deliveries": status.get("deliveries"),
                    },
                    indent=2,
                )
            )
            return

        if args.action == "activity":
            response = client.post("/activity")
        else:
            response = client.get(
                {"read": "/history", "status": "/status", "inspect": "/inspect"}[args.action]
            )

        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))


def save(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
