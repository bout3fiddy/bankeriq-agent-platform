"""Bounded authenticated reconnect observation while Terraform triggers hosted failover."""

import argparse
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx2


def observe(base: str, audience: str, source: Path, stop: Path, output: Path) -> None:
    if output.exists() or stop.exists():
        raise ValueError("choose fresh evidence and stop paths")

    record = json.loads(source.read_text())["requests"][0]
    response = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "api://" + audience, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    token = json.loads(response.stdout)["accessToken"]
    statuses = Counter()
    failure_started = None
    longest_failure = 0.0
    started = time.monotonic()
    last_ok = False

    with httpx2.Client(
        base_url=base,
        timeout=5,
        headers={
            "authorization": "Bearer " + token,
            "x-conversation-id": record["conversation_id"],
            "x-run-id": record["run_id"],
        },
    ) as client:
        while time.monotonic() - started < 900:
            try:
                result = client.get("/status")
                code = str(result.status_code)
                last_ok = result.status_code == 200

                if last_ok:
                    assert result.json()["status"] == "completed"
            except httpx2.TransportError:
                code = "transport_error"
                last_ok = False

            statuses[code] += 1

            # A database outage may temporarily make the owned view unavailable.
            # The same acknowledged identity must become completed again; there
            # is no new admission, fresh operation ID or reset of execution budgets.
            if last_ok:
                stop.with_suffix(".ready").touch()

                if failure_started is not None:
                    longest_failure = max(longest_failure, time.monotonic() - failure_started)
                    failure_started = None
            elif failure_started is None:
                failure_started = time.monotonic()

            if stop.exists():
                break

            time.sleep(0.5)

    assert last_ok, "accepted request did not recover before observation ended"
    output.write_text(
        json.dumps(
            {
                "responses": dict(statuses),
                "longest_observed_failure_seconds": longest_failure,
                "duration_seconds": time.monotonic() - started,
                "accepted_request_preserved": True,
            },
            indent=2,
        )
        + "\n"
    )
    print(output.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observe(args.url, args.audience, args.source, args.stop, args.output)
