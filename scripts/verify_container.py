"""Validate a locally built synthetic image through Docker and its published HTTP port."""

import argparse
import json
import os
import secrets
import subprocess
import time
from uuid import uuid4

import httpx2

from tests.validation.application import PROMPT
from tests.validation.tools import PRIVATE_MARKER


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bankeriq-runtime:synthetic")
    arguments = parser.parse_args()
    image = arguments.image

    # Resolve once so every check uses the same local image, even if its tag moves.
    image_id = docker("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()

    # First run the synthetic workflow with networking disabled. A successful
    # result establishes that the image contains its offline dependencies and can
    # complete the calculation/report scenario under the restricted filesystem.
    # The separate server phase below checks loopback HTTP and graceful shutdown.
    isolation = ["--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"]
    validated = docker("run", "--rm", "--network=none", *isolation, image_id, "validate")
    report = json.loads(validated.stdout)
    assert report["status"] == "passed"
    assert (report["model_steps"], report["tool_calls"], report["job_resumptions"]) == (4, 3, 1)

    missing_token = docker("run", "--rm", "--network=none", *isolation, image_id, check=False)
    assert missing_token.returncode == 2
    assert "Startup configuration failed" in missing_token.stderr

    name = f"bankeriq-smoke-{uuid4().hex}"
    token = secrets.token_urlsafe(32)
    environment = {**os.environ, "BANKERIQ_VALIDATION_TOKEN": token}

    try:

        # Docker receives the value from the environment, keeping the token out
        # of process arguments. Publish only on the local machine's loopback.
        docker(
            "run",
            "--detach",
            "--name",
            name,
            *isolation,
            "--publish",
            "127.0.0.1::8088",
            "--env",
            "BANKERIQ_VALIDATION_TOKEN",
            image_id,
            environment=environment,
        )
        address = docker("port", name, "8088/tcp").stdout.strip()
        deadline = time.monotonic() + 30

        while (
            docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
            != "healthy"
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("container did not become healthy")

            time.sleep(0.25)

        user = docker("exec", name, "python", "-c", "import os; print(os.getuid())")
        assert user.stdout.strip() == "10001"

        with httpx2.Client(base_url=f"http://{address}", timeout=5) as client:
            verify_http(client, token)

        docker("stop", "--time", "10", name)
        exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip()
        assert exit_code == "0", f"container exit code: {exit_code}"
        logs = docker("logs", name)
        assert token not in logs.stdout + logs.stderr
        assert PRIVATE_MARKER not in logs.stdout + logs.stderr
    finally:
        docker("rm", "--force", name, check=False)

    print(f"Container validation passed: {image_id}")
    print(
        "Offline workflow, required token, readiness, non-root HTTP server, two turns and shutdown."
    )


def verify_http(client: httpx2.Client, token: str) -> None:
    assert client.get("/readiness").status_code == 200
    assert client.post("/invocations", json={"action": "create_conversation"}).status_code == 401
    client.headers["Authorization"] = f"Bearer {token}"

    def invoke(command: dict) -> dict:
        response = client.post("/invocations", json=command)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        return response.json()

    conversation = invoke({"action": "create_conversation"})
    revision = 0

    # Reuse one conversation for two fresh runs. The first completed response
    # supplies the revision used to admit the second turn, exercising continuity
    # through the HTTP boundary. Each turn first submits unapproved text and must
    # receive 403 before the same command is retried with the approved validation request.
    for _ in range(2):
        run_id = str(uuid4())
        command = {
            "action": "start_run",
            "conversation_id": conversation["conversation_id"],
            "run_id": run_id,
            "expected_conversation_revision": revision,
            "message": "Arbitrary user content",
        }
        assert client.post("/invocations", json=command).status_code == 403
        command["message"] = PROMPT
        result = invoke(command)
        assert result["status"] == "waiting"
        result = invoke(
            {
                "action": "resume_run",
                "conversation_id": conversation["conversation_id"],
                "run_id": run_id,
                **result["pending"][0],
            }
        )
        assert result["status"] == "completed"
        assert "42" in result["answer"]
        assert len(result["artifacts"]) == 1
        revision = result["conversation_revision"]


def docker(
    *arguments: str, environment: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        timeout=60,
    )


if __name__ == "__main__":
    main()
