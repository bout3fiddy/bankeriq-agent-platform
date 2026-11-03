"""Host the runtime with Microsoft's Invocations protocol and injected authentication."""

from collections.abc import Awaitable, Callable

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_harness.conversations.context import ContextBudgetExceeded
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.hosting.access import AuthenticationRequired, InputRejected, Principal
from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.hosting.requests import InvalidInvocation, Invocation, parse_invocation


def create_hosted_server(
    endpoint: RuntimeEndpoint,
    *,
    authenticate: Callable[[Request], Awaitable[Principal]],
) -> InvocationAgentServerHost:
    """Build an ASGI host with POST /invocations and the SDK's GET /readiness."""

    # The SDK can enable message-content telemetry through environment settings.
    # Application observability needs an explicit data policy before enabling it.
    server = InvocationAgentServerHost(configure_observability=None, access_log=None)

    @server.invoke_handler
    async def invoke(request: Request) -> JSONResponse:
        try:

            # Example local fake, passed as authenticate=local_identity:
            # async def local_identity(request: Request) -> Principal:
            #     return Principal("demo-tenant", "demo-user")
            #
            # Production verification must bind the enterprise tenant/user to
            # trusted platform identity. Client-supplied headers are insufficient.
            principal = await authenticate(request)
            command = await _read_command(request)

            # The coordinator currently blocks during model/tool calls. Running
            # it in the server's bounded worker pool lets other sessions proceed
            # on the event loop. Store revisions still serialize a conversation.
            body = await run_in_threadpool(endpoint.invoke, principal, command)
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        except AuthenticationRequired:
            return _error(401, "authentication_required")
        except InputRejected:
            return _error(403, "input_rejected")
        except InvalidInvocation:
            return _error(400, "invalid_request")
        except StateNotFoundError:
            return _error(404, "state_unavailable")
        except ContextBudgetExceeded:
            return _error(422, "context_limit_exceeded")
        except StateConflictError:
            return _error(409, "state_conflict")
        except Exception:

            # The hosting SDK logs uncaught handler exceptions. Keep dependency
            # messages out of that path and return a fixed application error.
            return _error(500, "runtime_unavailable")

    return server


async def _read_command(request: Request) -> Invocation:
    chunks: list[bytes] = []
    size = 0

    # Bound body buffering even when Content-Length is absent or inaccurate.
    # This text-only interface accepts at most 64 KiB per invocation.
    async for chunk in request.stream():
        size += len(chunk)

        if size > 65_536:
            raise InvalidInvocation("invocation too large")

        chunks.append(chunk)

    return parse_invocation(b"".join(chunks))


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})
