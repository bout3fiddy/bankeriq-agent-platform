"""Exercise the deployed pilot using an in-memory delegated Entra access token."""

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx2


def check(base: str, audience: str, output: Path, turns: int, report: bool = False) -> None:

    # Never put an access token in argv, an output artifact or a transcript.
    # Azure CLI obtains it in memory; httpx uses the verified TLS API endpoint.
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "api://" + audience, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    token = json.loads(result.stdout)["accessToken"]

    if output.exists():
        raise ValueError("choose a new evidence file")

    latencies = []
    records = []
    children = []
    text = (
        "Calculate the sum of 2 and 3, then prepare a restricted report."
        if report
        else "Calculate the sum of 2 and 3."
    )

    with httpx2.Client(base_url=base, timeout=30) as client:
        assert client.get("/status").status_code == 401
        assert client.get("/reports/synthetic").status_code == 401
        client.headers["authorization"] = "Bearer " + token
        assert client.get("/reports/synthetic").status_code == 200
        response = client.post("/conversations")
        assert response.status_code == 200, response.status_code
        chat = response.json()["conversation_id"]
        client.headers["x-conversation-id"] = chat

        for _ in range(turns):
            run = str(uuid4())
            client.headers["x-run-id"] = run
            rejected = client.post("/runs", json={"request_id": run, "text": "unreviewed input"})
            assert rejected.status_code == 400
            payload = {"request_id": run, "text": text}
            started = time.monotonic()
            admitted = client.post("/runs", json=payload)
            assert admitted.status_code == 202, admitted.status_code
            assert client.post("/runs", json=payload).status_code == 202
            record = {"conversation_id": chat, "run_id": run, "text": text}
            records.append(record)
            output.write_text(json.dumps({"requests": records, "status": "running"}) + "\n")

            for _ in range(120):
                response = client.get("/status")
                assert response.status_code == 200, response.status_code
                view = response.json()
                assert "SYNTHETIC_PRIVATE" not in response.text

                if view["status"] in ("completed", "failed", "cancelled"):
                    break

                time.sleep(1)

            assert view["status"] == "completed", view["status"]
            assert view["children"] and all(
                child["status"] == "completed" for child in view["children"]
            )

            if report:
                assert view["deliveries"], "report delivery missing"

            record["completed_children"] = len(view["children"])
            children.append(len(view["children"]))
            latencies.append(time.monotonic() - started)

    output.write_text(
        json.dumps(
            {
                "requests": records,
                "completed_turns": len(latencies),
                "seconds": latencies,
                "completed_children": children,
                "report_delivery_checked": report,
                "unauthenticated_requests_rejected": True,
                "input_policy_enforced": True,
                "duplicate_admission_accepted": True,
                "report_authentication_enforced": True,
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    check(args.url, args.audience, args.output, args.turns, args.report)
