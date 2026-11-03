"""Verify previously acknowledged synthetic requests after restart, failover or restore."""

import argparse
import json
import subprocess
from pathlib import Path

import httpx2


def check(base: str, audience: str, sources: list[Path], output: Path) -> None:
    if output.exists():
        raise ValueError("choose a new evidence file")

    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "api://" + audience, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    token = json.loads(result.stdout)["accessToken"]
    checked = 0

    with httpx2.Client(base_url=base, headers={"authorization": "Bearer " + token}) as client:
        for source in sources:
            for record in json.loads(source.read_text())["requests"]:
                client.headers["x-conversation-id"] = record["conversation_id"]
                client.headers["x-run-id"] = record["run_id"]
                response = client.get("/status")
                response.raise_for_status()
                view = response.json()
                assert view["status"] == "completed"
                assert len(view["children"]) == record["completed_children"]
                assert all(child["attempt"] == 1 for child in view["children"])

                # Resolve the original identity after reconnecting. This must not
                # create a second child or another accepted conversation turn.
                repeated = client.post(
                    "/runs", json={"request_id": record["run_id"], "text": record["text"]}
                )
                assert repeated.status_code == 202
                again = client.get("/status").json()
                assert again["revision"] == view["revision"]
                assert again["children"] == view["children"]
                checked += 1

    evidence = {
        "acknowledged_requests_preserved": checked,
        "duplicate_replay_unchanged": True,
        "tool_attempts_each": 1,
    }
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(output.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.url, args.audience, args.source, args.output)
