"""Launch the installed command in a fresh process and exercise its local boundary."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx2

from bankeriq_agent_platform.demo.application import PROMPT


def test_mock_validation_runs_in_a_fresh_process(tmp_path: Path) -> None:
    transcript = tmp_path / "conversation.md"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bankeriq_agent_platform",
            "validate",
            "--transcript",
            str(transcript),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["model_steps"] == 4
    assert report["tool_calls"] == 3
    assert report["job_resumptions"] == 1
    assert "Model selection: mock/synthetic" in transcript.read_text()

    # Reusing the same output path must preserve the earlier evidence. The
    # collision is detected before opening a model connection or starting a run.
    original = transcript.read_bytes()
    repeated = subprocess.run(result.args, capture_output=True, text=True, timeout=30)
    assert repeated.returncode == 2
    assert transcript.read_bytes() == original


def test_server_startup_authentication_two_turns_and_shutdown() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    token = secrets.token_urlsafe(32)
    environment = {**os.environ, "BANKERIQ_DEMO_TOKEN": token}
    process = subprocess.Popen(
        [sys.executable, "-m", "bankeriq_agent_platform", "serve", "--port", str(port)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            deadline = time.monotonic() + 15

            while True:
                try:
                    if client.get("/readiness").status_code == 200:
                        break
                except httpx2.TransportError:
                    pass

                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("demo server did not become ready")

                time.sleep(0.05)

            assert (
                client.post("/invocations", json={"action": "create_conversation"}).status_code
                == 401
            )
            client.headers["Authorization"] = f"Bearer {token}"
            conversation = client.post(
                "/invocations", json={"action": "create_conversation"}
            ).json()
            revision = 0

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
                result = client.post("/invocations", json=command).json()
                assert result["status"] == "waiting"
                result = client.post(
                    "/invocations",
                    json={
                        "action": "resume_run",
                        "conversation_id": conversation["conversation_id"],
                        "run_id": run_id,
                        **result["pending"][0],
                    },
                ).json()
                assert result["status"] == "completed"
                assert "42" in result["answer"]
                assert len(result["artifacts"]) == 1
                revision = result["conversation_revision"]
    finally:
        process.terminate()

        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError("demo server did not shut down") from None

    assert process.returncode == 0, stderr
    assert token not in stdout + stderr


def test_serve_requires_an_explicit_token() -> None:
    environment = {key: value for key, value in os.environ.items() if key != "BANKERIQ_DEMO_TOKEN"}
    result = subprocess.run(
        [sys.executable, "-m", "bankeriq_agent_platform", "serve"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "Startup configuration failed" in result.stderr
