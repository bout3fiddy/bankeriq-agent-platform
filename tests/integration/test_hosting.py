"""Exercise the Microsoft hosting SDK locally with synthetic identity and input policy."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_runtime.coordinator.model import ModelReply, ModelRequest
from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.hosting.access import AuthenticationRequired, InputRejected, Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.prompts.resolution import FixedPromptResolver
from agent_runtime.providers.foundry.hosting import create_hosted_server
from agent_runtime.runs.context import RunContext
from agent_runtime.tools.definitions import ToolCall

from .harness import CALCULATE, CONTEXT, PRIVATE_BODY, REPORT, RuntimeHarness


async def fake_authenticate(request: Request) -> Principal:
    identities = {
        "Bearer owner": Principal("tenant", "user"),
        "Bearer other": Principal("tenant", "other"),
        "Bearer other-tenant": Principal("other-tenant", "user"),
    }
    principal = identities.get(request.headers.get("authorization", ""))

    if principal is None:
        raise AuthenticationRequired()

    return principal


def client_for(
    runtime: RuntimeHarness,
    *,
    prepare_input: Callable[[Principal, str], str] = lambda principal, text: text,
) -> TestClient:
    endpoint = RuntimeEndpoint(
        runtime.store, runtime.coordinator, runtime.continuations, prepare_input=prepare_input
    )
    app = create_hosted_server(endpoint, authenticate=fake_authenticate)
    return TestClient(app, headers={"Authorization": "Bearer owner"})


def create_conversation(client: TestClient) -> str:
    response = client.post("/invocations", json={"action": "create_conversation"})
    assert response.status_code == 200
    assert response.json()["revision"] == 0
    return response.json()["conversation_id"]


def start_body(
    conversation_id: str, *, run_id: str = "run", revision: int = 0
) -> dict[str, object]:
    return {
        "action": "start_run",
        "conversation_id": conversation_id,
        "run_id": run_id,
        "expected_conversation_revision": revision,
        "message": "Calculate and prepare report",
    }


def test_http_create_start_approval_job_and_reply() -> None:
    runtime = RuntimeHarness()
    runtime.model.replies = [
        ModelReply(calls=(ToolCall("a", CALCULATE.name, {"values": [12, 30]}),)),
        ModelReply(calls=(ToolCall("b", REPORT.name, {"dataset": "sample", "total": 42}),)),
        ModelReply(answer="The total is 42; your report is ready."),
        ModelReply(answer="Follow-up answered."),
    ]

    with client_for(runtime) as client:
        assert client.get("/readiness").status_code == 200
        conversation_id = create_conversation(client)
        context = replace(CONTEXT, conversation_id=conversation_id)
        runtime.contexts.append(context)
        waiting = client.post("/invocations", json=start_body(conversation_id))
        assert waiting.status_code == 200
        assert waiting.json()["status"] == "waiting"
        assert waiting.json()["answer"] is None
        approval = runtime.store.load_run(context).run.pending_calls[0]

        # The trusted approval provider records the user's decision. The HTTP
        # event carries only the reference needed to look up that decision.
        runtime.events.decide(context, approval, granted=True)
        resume = {
            "action": "resume_run",
            "conversation_id": conversation_id,
            "run_id": "run",
            **waiting.json()["pending"][0],
        }
        queued = client.post("/invocations", json=resume)
        assert queued.json()["status"] == "waiting"
        assert queued.json()["pending"][0]["reason"] == "job"
        assert len(runtime.model.requests) == 2
        resume.update(queued.json()["pending"][0])
        completed = client.post("/invocations", json=resume)
        body = completed.json()
        saved = runtime.store.load_run(context)
        assert body["status"] == "completed"
        assert body["answer"] == "The total is 42; your report is ready."
        assert body["conversation_revision"] == saved.conversation.revision
        assert body["pending"] == []
        assert PRIVATE_BODY not in completed.text
        assert completed.headers["cache-control"] == "no-store"
        artifact_id = body["artifacts"][0]["artifact_id"]
        assert runtime.read_report(context, artifact_id) == f"{PRIVATE_BODY}: total=42"

        # Replaying an event cannot execute the report again.
        assert client.post("/invocations", json=resume).status_code == 409
        assert runtime.executions == [(CALCULATE.name, context), (REPORT.name, context)]
        assert runtime.store.load_run(context) == saved

        followup = client.post(
            "/invocations",
            json=start_body(
                conversation_id, run_id="follow-up", revision=body["conversation_revision"]
            ),
        )
        assert followup.json()["answer"] == "Follow-up answered."

    runtime.assert_public_boundaries()


def test_authentication_and_identity_spoofing_fail_before_runtime_work() -> None:
    runtime = RuntimeHarness()

    with client_for(runtime) as client:
        rejected = client.post(
            "/invocations",
            json={"action": "create_conversation"},
            headers={
                "Authorization": "",
                "x-agent-user-id": "user",
                "x-client-tenant-id": "tenant",
            },
        )
        assert rejected.status_code == 401
        conversation_id = create_conversation(client)
        body = start_body(conversation_id)
        assert client.post("/invocations", json={**body, "user_id": "user"}).status_code == 400

        for token in ("other", "other-tenant"):
            response = client.post(
                "/invocations", json=body, headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 404
            assert response.json() == {"error": "state_unavailable"}

    assert runtime.model.requests == []


def test_input_policy_transforms_text_before_it_enters_history() -> None:
    runtime = RuntimeHarness()
    runtime.model.replies = [ModelReply(answer="Done")]

    def prepare_input(principal: Principal, text: str) -> str:
        assert principal == Principal("tenant", "user")
        return "Admitted synthetic prompt"

    with client_for(runtime, prepare_input=prepare_input) as client:
        conversation_id = create_conversation(client)
        response = client.post(
            "/invocations", json={**start_body(conversation_id), "message": PRIVATE_BODY}
        )

    assert response.status_code == 200
    assert PRIVATE_BODY not in runtime.model.request_payloads[0]
    assert "Admitted synthetic prompt" in runtime.model.request_payloads[0]


@pytest.mark.parametrize(
    "error,status", [(InputRejected(PRIVATE_BODY), 403), (RuntimeError(PRIVATE_BODY), 500)]
)
def test_policy_rejections_and_errors_leave_no_run_or_raw_logs(
    error: Exception, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = RuntimeHarness()

    def prepare_input(principal: Principal, text: str) -> str:
        raise error

    with client_for(runtime, prepare_input=prepare_input) as client:
        conversation_id = create_conversation(client)
        response = client.post("/invocations", json=start_body(conversation_id))

    assert response.status_code == status
    assert PRIVATE_BODY not in response.text
    assert PRIVATE_BODY not in caplog.text
    conversation = runtime.store.load_conversation(
        tenant_id="tenant", user_id="user", conversation_id=conversation_id
    )
    assert conversation.revision == 0
    assert conversation.active_run_id is None
    assert runtime.model.requests == []


def test_stale_revision_and_duplicate_run_do_not_repeat_model_calls() -> None:
    runtime = RuntimeHarness()
    runtime.model.replies = [ModelReply(answer="Done")]

    with client_for(runtime) as client:
        conversation_id = create_conversation(client)
        result = client.post("/invocations", json=start_body(conversation_id)).json()
        stale = client.post("/invocations", json=start_body(conversation_id, run_id="next"))
        duplicate = client.post(
            "/invocations",
            json=start_body(conversation_id, revision=result["conversation_revision"]),
        )

    assert stale.status_code == 409
    assert duplicate.status_code == 409
    assert len(runtime.model.requests) == 1


def test_oversized_body_is_rejected_before_parsing_or_model_work() -> None:
    runtime = RuntimeHarness()

    with client_for(runtime) as client:
        response = client.post("/invocations", content=b"x" * 65_537)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert runtime.model.requests == []


def test_two_sessions_reach_blocking_model_calls_concurrently() -> None:
    runtime = RuntimeHarness()
    barrier = Barrier(2)

    class ConcurrentModel:
        def complete(self, request: ModelRequest) -> ModelReply:
            barrier.wait(timeout=3)
            return ModelReply(answer="Done")

    runtime.coordinator = Coordinator(
        runtime.store,
        runtime.registry,
        runtime.events,
        ConcurrentModel(),
        runtime.access.permissions_for,
        prompts=FixedPromptResolver("Synthetic test instructions"),
    )
    runtime.continuations = ContinuationHandler(runtime.store, runtime.coordinator, runtime.events)

    with client_for(runtime) as client, ThreadPoolExecutor(max_workers=2) as workers:
        conversations = [create_conversation(client), create_conversation(client)]
        futures = [
            workers.submit(
                client.post, "/invocations", json=start_body(conversation_id, run_id=f"run-{index}")
            )
            for index, conversation_id in enumerate(conversations)
        ]
        responses = [future.result(timeout=5) for future in futures]

    assert [response.json()["status"] for response in responses] == ["completed", "completed"]

    for index, conversation_id in enumerate(conversations):
        saved = runtime.store.load_run(
            RunContext("tenant", "user", conversation_id, f"run-{index}")
        )
        assert saved.run.model_steps == 1
