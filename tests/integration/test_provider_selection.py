"""Swap model factories while exercising the same Foundry host and runtime policies."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

import pytest
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.coordinator.model import ModelClient, ModelReply, ReasoningEffort
from agent_harness.tools.definitions import ToolCall
from agent_platform.composition import open_runtime
from agent_platform.configuration import load_settings
from agent_runtime.hosting.access import Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.providers.hosts import Authenticator, RuntimeServer
from agent_runtime.providers.models import ModelFactory, ModelSettings

from .harness import (
    CALCULATE,
    CONTEXT,
    PRIVATE_BODY,
    REPORT,
    RuntimeHarness,
    ScriptedModel,
    inspect_fields,
)


async def local_identity(request: Request) -> Principal:
    return Principal("tenant", "user")


@pytest.mark.parametrize("provider", ["first", "second"])
def test_model_selection_preserves_hosting_permissions_waits_and_output_policy(
    provider: str,
) -> None:
    runtime = RuntimeHarness()
    opened: list[str] = []
    closed: list[str] = []
    clients: list[ScriptedModel] = []

    def factory_for(name: str) -> ModelFactory:
        @contextmanager
        def factory(settings: ModelSettings) -> Iterator[ModelClient]:
            opened.append(name)
            model = ScriptedModel(
                replies=[
                    ModelReply(calls=(ToolCall("a", CALCULATE.name, {"values": [12, 30]}),)),
                    ModelReply(
                        calls=(ToolCall("b", REPORT.name, {"dataset": "sample", "total": 42}),)
                    ),
                    ModelReply(answer=f"Report prepared by {name}"),
                ]
            )
            clients.append(model)

            try:
                yield model
            finally:
                closed.append(name)

        return factory

    settings = load_settings(
        {
            "MODEL_PROVIDER": provider,
            "MODEL_NAME": "synthetic",
            "MODEL_REASONING_EFFORT": "high",
        }
    )

    # Only MODEL_PROVIDER changes between these cases. Hosting, permission
    # checks, output enforcement and continuation wiring stay the same.
    with open_runtime(
        settings,
        environment={},
        store=runtime.store,
        registry=runtime.registry,
        gateway=runtime.events,
        permissions_for=runtime.access.permissions_for,
        continuation_resolver=runtime.events,
        authenticate=local_identity,
        prepare_input=lambda principal, text: text,
        models={"first": factory_for("first"), "second": factory_for("second")},
    ) as server:
        assert isinstance(server, InvocationAgentServerHost)

        with TestClient(server) as client:
            created = client.post("/invocations", json={"action": "create_conversation"}).json()
            context = replace(CONTEXT, conversation_id=created["conversation_id"])
            waiting = client.post(
                "/invocations",
                json={
                    "action": "start_run",
                    "conversation_id": context.conversation_id,
                    "run_id": context.run_id,
                    "expected_conversation_revision": 0,
                    "message": "Prepare report",
                },
            ).json()
            assert waiting["status"] == "waiting"
            approval = runtime.store.load_run(context).run.pending_calls[0]
            runtime.events.decide(context, approval, granted=True)
            event = {
                "action": "resume_run",
                "conversation_id": context.conversation_id,
                "run_id": context.run_id,
                **waiting["pending"][0],
            }
            queued = client.post("/invocations", json=event).json()
            assert queued["status"] == "waiting"
            event.update(queued["pending"][0])
            completed = client.post("/invocations", json=event).json()

        assert completed["answer"] == f"Report prepared by {provider}"
        assert completed["status"] == "completed"
        assert opened == [provider]
        assert closed == []
        assert [request.reasoning_effort for request in clients[0].requests] == [
            ReasoningEffort.HIGH
        ] * 3
        artifact_id = completed["artifacts"][0]["artifact_id"]

        for payload in clients[0].request_payloads:
            assert PRIVATE_BODY not in payload
            assert artifact_id not in payload

        assert PRIVATE_BODY not in inspect_fields(runtime.store.load_run(context).conversation)

    assert closed == [provider]


def test_host_construction_failure_closes_the_opened_model() -> None:
    runtime = RuntimeHarness()
    closed: list[bool] = []

    @contextmanager
    def model_factory(settings: ModelSettings) -> Iterator[ModelClient]:
        try:
            yield runtime.model
        finally:
            closed.append(True)

    def broken_host(endpoint: RuntimeEndpoint, authenticate: Authenticator) -> RuntimeServer:
        raise RuntimeError("host unavailable")

    settings = load_settings(
        {"MODEL_PROVIDER": "local", "MODEL_NAME": "synthetic", "HOSTING_PROVIDER": "broken"}
    )

    with (
        pytest.raises(RuntimeError, match="host unavailable"),
        open_runtime(
            settings,
            environment={},
            store=runtime.store,
            registry=runtime.registry,
            gateway=runtime.events,
            permissions_for=runtime.access.permissions_for,
            continuation_resolver=runtime.events,
            authenticate=local_identity,
            prepare_input=lambda principal, text: text,
            models={"local": model_factory},
            hosts={"broken": broken_host},
        ),
    ):
        pytest.fail("host startup must fail")

    assert closed == [True]


def test_unsupported_host_fails_before_azure_credentials_are_read() -> None:
    runtime = RuntimeHarness()
    settings = load_settings({"MODEL_NAME": "coordinator", "HOSTING_PROVIDER": "unknown"})

    with (
        pytest.raises(ValueError, match="unsupported hosting provider"),
        open_runtime(
            settings,
            environment={},
            store=runtime.store,
            registry=runtime.registry,
            gateway=runtime.events,
            permissions_for=runtime.access.permissions_for,
            continuation_resolver=runtime.events,
            authenticate=local_identity,
            prepare_input=lambda principal, text: text,
        ),
    ):
        pytest.fail("host selection must fail")
