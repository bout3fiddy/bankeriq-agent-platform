"""Disposable Container Apps entry point using real runtime queues and synthetic tools."""

import asyncio
import json
import os
import signal
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import replace
from uuid import UUID, uuid4

import certifi
import psycopg
import uvicorn
from azure.identity import ManagedIdentityCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncCredential
from azure.identity.aio import get_bearer_token_provider
from openai import AsyncOpenAI
from psycopg.conninfo import make_conninfo
from redis import Redis
from redis_entraid.cred_provider import create_from_managed_identity
from redis_entraid.identity_provider import ManagedIdentityIdType, ManagedIdentityType
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_runtime.conversations.semantic import TokenBudget
from agent_runtime.prompts.resolution import FixedPromptResolver, PromptScope
from agent_runtime.providers.foundry.model import AsyncFoundryModelClient
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from agent_runtime.runs.context import RunContext
from agent_runtime.tools.bindings import ExecutionOutput, ToolBinding
from agent_runtime.tools.definitions import OutputPolicy
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.registry import ToolRegistry
from bankeriq_agent_platform.azure.authentication import (
    EntraAuthentication,
    operator_test_input,
    synthetic_input,
)
from bankeriq_agent_platform.azure.connections import token_connection
from bankeriq_agent_platform.memory.retrieval import HISTORY_TOOL, HistoryRetrieval
from bankeriq_agent_platform.memory.service import HistoryPolicy, SemanticMemory
from bankeriq_agent_platform.memory.storage import SemanticStore
from bankeriq_agent_platform.runtime import compose_queued_runtime
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.storage.connections import PrimaryConnections
from bankeriq_agent_platform.storage.postgres import CommitUnknownError, PostgresStateStore
from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver
from bankeriq_agent_platform.telemetry import ACTIVE_RUN, configure, emit
from bankeriq_agent_platform.workers.api import create_async_app
from bankeriq_agent_platform.workers.asynchronous import AsyncWorker
from bankeriq_agent_platform.workers.coordinator import QueuedCoordinator
from bankeriq_agent_platform.workers.state import Handlers
from tests.azure.migration import initialise
from tests.validation.tools import (
    CALCULATE,
    REPORT,
    calculate,
    public_total,
    validate_total,
    validate_values,
)


class Access:
    def __init__(self, tenant, users):
        self.tenant, self.users = tenant, users

    def permissions_for(self, context):
        return (
            frozenset({"validation.use", "history.read"})
            if (context.tenant_id == self.tenant and context.user_id in self.users)
            else frozenset()
        )

    def can_access(self, context, definition, call):
        return bool(self.permissions_for(context))

    def consume_approval(self, context, definition, call):
        return False


class NoModel:
    async def complete(self, request):
        raise RuntimeError("this process has no inference authority")


def observe_model(record):
    emit(
        "model.request",
        ACTIVE_RUN.get(),
        elapsed_ms=record.elapsed_ms,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        status_code=record.status_code,
        token_limit=record.token_limit,
        tokens_remaining=record.tokens_remaining,
        summary=record.summary,
    )


def composition(store, prompts, model, access, tool_delay: float = 0, *, summarizer=None):
    def execute_calculation(context, arguments):

        # A bounded synthetic delay makes active lease renewal and platform drain
        # observable. AsyncWorker runs this blocking fixture in its bounded executor.
        time.sleep(tool_delay)
        return calculate(context, arguments)

    # Every queued conversation uses durable memory. Startup owns the clients;
    # API/tool roles supply NoModel and only the coordinator can perform inference.
    # A source policy derives from the same current grants used by the gateway.
    # Changing the allowlist removes access before either summary reuse or retrieval.
    counter = FoundryTokenCounter("gpt-4o-2024-11-20")
    registry = ToolRegistry((CALCULATE, REPORT, HISTORY_TOOL))

    def history_access(context):
        grants = access.permissions_for(context)
        return HistoryPolicy(
            "pilot-memory-1:" + ",".join(sorted(grants)),
            frozenset(
                tool.name
                for tool in registry.discover(grants)
                if tool.output_policy == OutputPolicy.VISIBLE
            ),
            lambda fact: "history.read" in grants,
        )

    memory = SemanticMemory(
        SemanticStore(store),
        summarizer if summarizer is not None else model,
        prompts=FixedPromptResolver(
            "Memory format version 2. Select at most 24 concise candidate identifiers. "
            "Prioritize user decisions, corrections, names, responsibilities, open questions "
            "and tool outcomes. Keep user identifiers, including names and nicknames. Omit routine "
            "acknowledgements and illustrative examples. Return raw JSON only."
        ),
        summary_budget=TokenBudget("gpt-4o-2024-11-20-summary-v3", 8192, 1024, counter),
        context_budget=TokenBudget("gpt-4o-2024-11-20-context-v1", 8192, 512, counter),
        access_for=history_access,
    )
    gateway = ToolGateway(
        registry,
        {
            ("retrieve_history", "1"): HistoryRetrieval(memory).binding(),
            ("calculate", "v1"): ToolBinding(execute_calculation, validate_values, public_total),
            ("build_report", "v1"): ToolBinding(
                lambda c, a: ExecutionOutput(
                    {"body": "SYNTHETIC_PRIVATE"}, artifact_id="synthetic"
                ),
                validate_total,
            ),
        },
        access,
    )
    runtime = compose_queued_runtime(
        store=store,
        registry=registry,
        gateway=gateway,
        model=model,
        permissions_for=access.permissions_for,
        prompts=prompts,
        bind=QueuedCoordinator.bind_tool,
        memory=memory,
    )
    original = runtime.bind

    # Both fixtures are deterministic and have no external effect. Persist that
    # replay policy with each intent and require the current handler to agree.
    def bind(context, call, definition, state):
        child = original(context, call, definition, state)
        return replace(child, recovery=child.recovery.model_copy(update={"replay_allowed": True}))

    runtime.bind = bind
    entries = dict(runtime.handlers().entries)
    entries["tool", 1] = replace(entries["tool", 1], replay_allowed=True)
    return runtime, Handlers(entries)


async def api(runtime, sessions, tenant, users):
    verifier = EntraAuthentication(
        tenant_id=tenant,
        audience=os.environ["ENTRA_AUDIENCE"],
        users=users,
        clients=frozenset({"04b07795-8ddb-461a-bbee-02f9e1bf7b46"}),
    )

    async def user(request):
        try:
            return await asyncio.to_thread(
                verifier.authenticate, request.headers.get("authorization", "")
            )
        except ValueError:
            raise HTTPException(401, "authentication required") from None

    async def authenticate(request):
        principal = await user(request)

        try:
            chat = str(UUID(request.headers["x-conversation-id"]))
            run = str(UUID(request.headers["x-run-id"]))
        except KeyError, ValueError:
            raise HTTPException(400, "invalid context") from None

        return RunContext(principal.tenant_id, principal.user_id, chat, run)

    mode = os.environ.get("INPUT_POLICY", "fixtures")

    if mode not in {"fixtures", "operator_test"}:
        raise ValueError("unsupported input policy")

    app = create_async_app(
        runtime,
        sessions,
        authenticate=authenticate,
        prepare_input=operator_test_input if mode == "operator_test" else synthetic_input,
    )

    async def conversation(request):
        principal = await user(request)
        chat = str(uuid4())
        await asyncio.to_thread(
            runtime.queue.store.create_conversation,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=chat,
        )
        return JSONResponse({"conversation_id": chat})

    async def status(request):
        context = await authenticate(request)

        try:
            view = await asyncio.to_thread(sessions.progress_view, context)
        except Exception:
            raise HTTPException(404, "run unavailable") from None

        return JSONResponse(view)

    async def report(request):

        # This is a tool-owned synthetic endpoint fixture. Possessing its URL
        # does not grant access; no storage credential or body reaches the model.
        await user(request)
        return JSONResponse({"report": "synthetic report fixture"})

    async def inspect(request):
        context = await authenticate(request)
        return JSONResponse(await asyncio.to_thread(sessions.inspect, context))

    async def health(request):
        return JSONResponse({"ready": True})

    app.routes.extend(
        [
            Route("/conversations", conversation, methods=["POST"]),
            Route("/status", status),
            Route("/inspect", inspect),
            Route("/reports/synthetic", report),
            Route("/health", health),
        ]
    )
    await uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8088, access_log=False, log_level="warning")
    ).serve()


async def main():
    configure()
    role = os.environ["RUNTIME_ROLE"]
    tenant = os.environ["AZURE_TENANT_ID"]
    users = frozenset(json.loads(os.environ["ALLOWED_USERS"]))

    async with AsyncExitStack() as stack:
        credential = stack.enter_context(
            ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
        )
        dsn = make_conninfo(
            host=os.environ["POSTGRES_HOST"],
            dbname="bankeriq",
            user=os.environ["POSTGRES_USER"],
            sslmode="verify-full",

            # Binary libpq's compiled default trust path differs from Debian's.
            # Pass the packaged public roots explicitly and still verify the host.
            sslrootcert=certifi.where(),
            connect_timeout=10,
        )
        connections = PrimaryConnections(
            dsn,
            size=4,
            connection_type=token_connection(
                lambda: (
                    credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
                )
            ),
        )
        stack.callback(connections.close)
        assert connections.pool is not None

        # Cold managed-identity exchange may take longer than normal pool waits.
        # Finish one bounded startup connection before opening admission/dispatch.
        connections.pool.resize(min_size=1, max_size=4)
        await asyncio.to_thread(connections.pool.wait, timeout=30)

        if role == "migration":
            await asyncio.to_thread(initialise, dsn, connections)
            print("migration completed", flush=True)
            return

        store = PostgresStateStore(dsn, connections=connections)
        prompts = PostgresPromptResolver(
            dsn, PromptScope("bankeriq", "pilot"), connections=connections
        )
        model = NoModel()

        if role == "coordinator":
            async_credential = await stack.enter_async_context(
                AsyncCredential(client_id=os.environ["AZURE_CLIENT_ID"])
            )
            token = get_bearer_token_provider(
                async_credential, "https://cognitiveservices.azure.com/.default"
            )
            client = await stack.enter_async_context(
                AsyncOpenAI(
                    base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],
                    api_key=token,
                    max_retries=0,
                    timeout=30,
                )
            )
            model = AsyncFoundryModelClient(
                client,
                deployment="coordinator",
                max_completion_tokens=2048,
                observer=observe_model,
            )

        tool_delay = float(os.environ.get("PILOT_TOOL_DELAY_SECONDS", "0"))

        if not 0 <= tool_delay <= 45:
            raise ValueError("synthetic tool delay must be between zero and 45 seconds")

        runtime, handlers = composition(store, prompts, model, Access(tenant, users), tool_delay)
        sessions = None

        if role in ("api", "coordinator"):
            provider = create_from_managed_identity(
                ManagedIdentityType.USER_ASSIGNED,
                "https://redis.azure.com",
                ManagedIdentityIdType.CLIENT_ID,
                os.environ["AZURE_CLIENT_ID"],
            )
            redis = Redis(
                host=os.environ["REDIS_HOST"],
                port=10000,
                ssl=True,
                credential_provider=provider,
                protocol=3,
                socket_connect_timeout=2,
                socket_timeout=2,
                max_connections=8,
            )
            cache = RedisSessions(
                "", client=redis, ttl_seconds=int(os.environ.get("SESSION_IDLE_SECONDS", "1800"))
            )
            stack.callback(cache.close)
            sessions = SessionService(store, cache)

        if role == "api":
            assert sessions is not None
            emit("runtime.ready")
            await api(runtime, sessions, tenant, users)
            return

        if role not in ("coordinator", "tool"):
            raise ValueError("unsupported runtime role")

        stop = asyncio.Event()

        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        kinds = ("model_prepare", "model_infer") if role == "coordinator" else ("tool",)

        async def maintenance():
            assert sessions is not None
            active_after = ("", "")

            while not stop.is_set():
                with suppress(psycopg.OperationalError, CommitUnknownError):

                    # Tool identities need no Redis access. The coordinator uses
                    # live PostgreSQL leases to renew existing views during long
                    # calls; a completed sweep restarts from the first scope.
                    _, next_scope = await asyncio.to_thread(
                        sessions.keep_active, after=active_after
                    )
                    active_after = next_scope or ("", "")
                    await asyncio.to_thread(sessions.project_due, limit=10)

                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), min(1, sessions.cache.ttl_seconds / 3))

        tasks = [
            AsyncWorker(
                runtime.queue,
                handlers,
                queue_name=kind,
                concurrency=2,
                lease_seconds=15,
                poll_seconds=0.25,
                process_limit=6,
                connection_budget=56,
                api_reserve=8,
            ).run_async(stop)
            for kind in kinds
        ]

        if sessions is not None:
            tasks.append(maintenance())

        emit("runtime.ready")
        await asyncio.gather(*tasks)
        emit("runtime.drained")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:

        # Provider errors may contain endpoint details. Keep console telemetry
        # to fixed lifecycle messages and an exception class, without payloads.
        print("pilot failed:", type(error).__name__, flush=True)
        raise SystemExit(1) from None
