"""Exercise the coordinator and output policies through the real SDK's HTTP boundary."""

import json

import httpx2
import pytest
from openai import OpenAI

from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.providers.foundry.model import FoundryModelClient
from agent_runtime.runs.state import RunStatus

from .harness import (
    CALCULATE,
    CONTEXT,
    PERMISSIONS,
    PRIVATE_BODY,
    REPORT,
    RuntimeHarness,
    event_for,
    inspect_fields,
)


def test_http_tool_cycles_approval_and_report_delivery() -> None:
    runtime = RuntimeHarness()
    requests: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append(payload)

        if len(requests) == 1:
            return _call_response("sum-1", CALCULATE.name, {"values": [12, 30]})

        if len(requests) == 2:
            result = json.loads(payload["messages"][-1]["content"])
            assert result == {"status": "completed", "result": {"total": 42}}
            return _call_response(
                "report-1", REPORT.name, {"dataset": "sample", "total": result["result"]["total"]}
            )

        assert json.loads(payload["messages"][-1]["content"]) == {
            "status": "completed",
            "output": "restricted",
        }
        return _response("stop", {"role": "assistant", "content": "The total is 42; report ready."})

    with OpenAI(
        base_url="https://synthetic.openai.azure.com/openai/v1/",
        api_key="synthetic-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as client:
        model = FoundryModelClient(client, deployment="coordinator", instructions="Use tools.")
        runtime.coordinator = Coordinator(
            runtime.store, runtime.registry, runtime.events, model, runtime.access.permissions_for
        )
        runtime.continuations = ContinuationHandler(
            runtime.store, runtime.coordinator, runtime.events
        )
        waiting = runtime.start()
        assert waiting.run.status is RunStatus.WAITING
        approval = waiting.run.pending_calls[0]
        decision = runtime.events.decide(CONTEXT, approval, granted=True)
        queued = runtime.continuations.resume(CONTEXT, decision)
        job = queued.run.pending_calls[0]
        assert len(requests) == 2

        # Changing permissions while waiting affects the next inference request.
        runtime.access.permissions[(CONTEXT.tenant_id, CONTEXT.user_id)] = PERMISSIONS - {
            "public.calculate"
        }
        completed = runtime.continuations.resume(CONTEXT, event_for(job))

    assert completed.run.status is RunStatus.COMPLETED
    assert completed.run.model_steps == 3
    assert completed.answer == "The total is 42; report ready."
    assert runtime.executions == [(CALCULATE.name, CONTEXT), (REPORT.name, CONTEXT)]
    assert [tool["function"]["name"] for tool in requests[2]["tools"]] == [REPORT.name]
    artifact = completed.artifacts[0]
    assert runtime.read_report(CONTEXT, artifact.artifact_id) == f"{PRIVATE_BODY}: total=42"
    wire = json.dumps(requests)

    for internal_value in (
        PRIVATE_BODY,
        artifact.artifact_id,
        approval.reference_id,
        job.reference_id,
    ):
        assert internal_value not in wire

    runtime.assert_public_boundaries()


@pytest.mark.parametrize("failure", ["truncated", "invalid_arguments", "rate_limit", "timeout"])
def test_provider_failures_end_the_run_before_tool_execution(failure: str) -> None:
    runtime = RuntimeHarness()
    attempts: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)

        if failure == "timeout":
            raise httpx2.ReadTimeout(PRIVATE_BODY, request=request)

        if failure == "rate_limit":
            return httpx2.Response(429, json={"error": {"message": PRIVATE_BODY}})

        if failure == "invalid_arguments":
            return _call_response("sum-1", CALCULATE.name, [PRIVATE_BODY])

        return _response("length", {"role": "assistant", "content": PRIVATE_BODY})

    with OpenAI(
        base_url="https://synthetic.openai.azure.com/openai/v1/",
        api_key="synthetic-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as client:
        model = FoundryModelClient(client, deployment="coordinator", instructions="Use tools.")
        runtime.coordinator = Coordinator(
            runtime.store, runtime.registry, runtime.events, model, runtime.access.permissions_for
        )
        result = runtime.start()

    assert result.run.status is RunStatus.FAILED
    assert result.run.model_steps == 1
    assert result.answer is None
    assert len(attempts) == 1
    assert runtime.executions == []
    assert PRIVATE_BODY not in inspect_fields(runtime.store.load_run(CONTEXT))
    runtime.assert_public_boundaries()


def _call_response(call_id: str, name: str, arguments: object) -> httpx2.Response:
    return _response(
        "tool_calls",
        {
            "role": "assistant",
            "content": "Working on that.",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
    )


def _response(finish_reason: str, message: dict[str, object]) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "completion",
            "created": 1,
            "model": "synthetic",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        },
    )
