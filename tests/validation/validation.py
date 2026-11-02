"""Bounded synthetic workflow through the real HTTP handlers and selected model."""

import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import TextIO
from uuid import uuid4

import httpx2

from agent_runtime.providers.foundry.hosting import create_hosted_server
from agent_runtime.runs.context import RunContext
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import RestrictedReceipt, VisibleResult

from .application import PRINCIPAL, PROMPT, LocalServer
from .tools import PRIVATE_MARKER, ValidationDependencies
from .transcript import write_transcript


class ValidationFailed(RuntimeError):
    """A completed smoke test did not demonstrate the required runtime behaviour."""


async def validate(
    server: LocalServer,
    dependencies: ValidationDependencies,
    token: str,
    *,
    transcript_file: TextIO | None = None,
    model_label: str = "unspecified",
) -> dict[str, object]:

    # ASGITransport exercises the actual authenticated HTTP handlers inside this
    # process. For example, invoke({"action": "create_conversation"}) sends the
    # generated bearer token and receives a real endpoint response. The transport
    # itself opens no listening socket; the selected model may still call Foundry.
    app = create_hosted_server(server.endpoint, authenticate=server.authenticate)
    transport = httpx2.ASGITransport(app=app)

    async with httpx2.AsyncClient(transport=transport, base_url="http://validation") as client:
        headers = {"Authorization": f"Bearer {token}"}

        async def invoke(command: dict[str, object]) -> dict:
            response = await client.post("/invocations", headers=headers, json=command)

            if response.status_code != 200:
                raise ValidationFailed(f"invocation failed with HTTP {response.status_code}")

            return response.json()

        created = await invoke({"action": "create_conversation"})
        run_id = str(uuid4())
        result = await invoke(
            {
                "action": "start_run",
                "conversation_id": created["conversation_id"],
                "run_id": run_id,
                "expected_conversation_revision": 0,
                "message": PROMPT,
            }
        )
        artifacts = list(result["artifacts"])
        waits = 0

        # Only saved validation job receipts can be resumed. A bounded loop also makes
        # a broken continuation visible instead of hanging a live validation run.
        while result["status"] == "waiting" and waits < 6:
            result = await invoke(
                {
                    "action": "resume_run",
                    "conversation_id": created["conversation_id"],
                    "run_id": run_id,
                    **result["pending"][0],
                }
            )
            artifacts.extend(result["artifacts"])
            waits += 1

    context = RunContext(PRINCIPAL.tenant_id, PRINCIPAL.user_id, created["conversation_id"], run_id)
    checkpoint = dependencies.store.load_run(context)
    history = checkpoint.conversation.items
    calls = [item for item in history if isinstance(item, ToolCall)]
    visible = [item for item in history if isinstance(item, VisibleResult)]

    # Serialize the stored fields rather than repr(history): message/result repr
    # deliberately hides payloads. A leaked report body or opaque artifact ID must
    # therefore be visible to this assertion before any transcript is exported.
    transcript = json.dumps(history, default=_json_fields)
    required_calls = ["sample_values", "calculate", "build_report"]

    if PRIVATE_MARKER in transcript or any(
        artifact["artifact_id"] in transcript for artifact in artifacts
    ):
        raise ValidationFailed("restricted content entered conversation history")

    # Preserve the observed exchange even if a later workflow assertion fails.
    # For example, a model that answers immediately leaves a transcript showing
    # the missing tool calls. Output-isolation failures block export above.
    if transcript_file is not None:
        assert checkpoint.run.prompt_manifest is not None
        write_transcript(
            transcript_file,
            checkpoint,
            instructions=dependencies.prompts.instructions(checkpoint.run.prompt_manifest),
            model_label=model_label,
            resumptions=waits,
        )

    if result["status"] != "completed" or not re.search(r"\b42\b", result["answer"] or ""):
        raise ValidationFailed(
            f"expected a completed answer containing 42; status={result['status']}"
        )

    if [call.tool_name for call in calls] != required_calls or checkpoint.run.model_steps < 4:
        raise ValidationFailed("expected three successive tool/model iterations")

    if (
        len(visible) != 2
        or visible[0].payload != {"values": (12, 30)}
        or visible[1].payload != {"total": 42}
    ):
        raise ValidationFailed("unexpected synthetic calculation results")

    if (
        waits != 1
        or len(artifacts) != 1
        or not any(isinstance(item, RestrictedReceipt) for item in history)
    ):
        raise ValidationFailed("expected a restricted report and one job continuation")

    return {
        "status": "passed",
        "model_steps": checkpoint.run.model_steps,
        "tool_calls": len(calls),
        "job_resumptions": waits,
        "answer": result["answer"],
    }


def _json_fields(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}

    if isinstance(value, Mapping):
        return dict(value)

    raise TypeError("unsupported transcript value")
