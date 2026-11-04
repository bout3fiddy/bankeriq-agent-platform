"""Small authenticated admission/SSE transport; no execution lives in HTTP requests."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent_harness.runs.context import RunContext
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.sessions.service import SessionService
from agent_runtime.sessions.stream import Cursor, ProgressStream
from agent_runtime.telemetry import emit
from agent_runtime.workers.coordinator import QueuedCoordinator
from agent_runtime.workers.releases import AdmissionOverloaded, AdmissionPaused


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=32768)
    worker_budget: int = Field(default=100, ge=1, le=100)


def create_async_app(
    runtime: QueuedCoordinator,
    sessions: SessionService,
    *,
    authenticate: Callable[[Request], Awaitable[RunContext]],
    prepare_input: Callable[[RunContext, str], str],
    max_streams: int = 64,
    streams_per_user: int = 8,
) -> Starlette:
    """Trusted authentication supplies the complete scoped context for each request.

    For example, authenticate(request) verifies a session and resolves its owned
    conversation/run from a route token. JSON cannot choose tenant or user identity.
    prepare_input performs application input policy before the accepted journal write.
    Enterprise authentication and the browser application remain separate integrations.
    """

    if not 1 <= streams_per_user <= max_streams <= 1024:
        raise ValueError("invalid streaming connection bounds")

    streams: dict[tuple[str, str], int] = {}

    async def admit(request: Request) -> Response:
        started = monotonic()
        context = await authenticate(request)
        body = bytearray()

        async for chunk in request.stream():
            body.extend(chunk)

            if len(body) > 65536:
                return JSONResponse({"error": "input_too_large"}, status_code=413)

        try:
            admission = Admission.model_validate_json(body)
            text = await asyncio.to_thread(prepare_input, context, admission.text)
            run_id = await runtime.admit(
                context,
                request_id=admission.request_id,
                text=text,
                worker_budget=admission.worker_budget,
            )
        except ValidationError, ValueError:
            emit("admission.rejected", context)
            return JSONResponse({"error": "invalid_input"}, status_code=400)
        except StateConflictError:
            return JSONResponse({"error": "conflict"}, status_code=409)
        except AdmissionPaused:
            return JSONResponse(
                {"error": "admission_paused"}, status_code=503, headers={"Retry-After": "2"}
            )
        except AdmissionOverloaded:
            return JSONResponse(
                {"error": "admission_capacity"}, status_code=429, headers={"Retry-After": "2"}
            )

        # Acceptance already committed. Cache/logging work cannot revoke that
        # receipt; a reconnect can recover the same request if Redis is down.
        emit("admission.accepted", context)
        emit(
            "runtime.span",
            context,
            stage="admission",
            elapsed_ms=round((monotonic() - started) * 1000),
        )

        with suppress(Exception):
            await asyncio.to_thread(sessions.record_activity, context)

        return JSONResponse({"accepted": True, "run_id": run_id}, status_code=202)

    async def cancel(request: Request) -> Response:
        context = await authenticate(request)

        # Ownership comes from the authenticated context and is checked again in
        # the primary transaction. A repeated cancellation returns the same state;
        # disconnecting an SSE reader never calls this explicit action.
        checkpoint = await asyncio.to_thread(runtime.queue.cancel, context)
        return JSONResponse({"run_id": context.run_id, "status": checkpoint.run.status.value})

    async def activity(request: Request) -> Response:
        context = await authenticate(request)
        renewed = await asyncio.to_thread(sessions.record_activity, context)
        return JSONResponse({"renewed": renewed})

    async def history(request: Request) -> Response:
        context = await authenticate(request)
        return JSONResponse(await asyncio.to_thread(sessions.read, context))

    async def unavailable(request: Request, error: Exception) -> Response:

        # Scope failures reveal neither another user's history nor database details.
        # The same handler covers inspection routes added by the hosting application.
        status = 404 if isinstance(error, StateNotFoundError) else 409
        return JSONResponse({"error": "session_unavailable"}, status_code=status)

    async def progress(request: Request) -> Response:
        context = await authenticate(request)
        raw_cursor = request.headers.get("last-event-id")

        try:
            if raw_cursor is not None and len(raw_cursor) > 512:
                raise ValueError("cursor exceeds bound")

            cursor = Cursor(**json.loads(raw_cursor)) if raw_cursor else None
            stream = ProgressStream(sessions, context, cursor=cursor)

            # Validate scope/generation before sending HTTP headers. Later polls
            # repeat ownership checks; deletion closes the existing stream.
            await asyncio.to_thread(stream.poll)
        except ValueError, TypeError, StateConflictError, StateNotFoundError:
            return JSONResponse({"error": "invalid_cursor_or_scope"}, status_code=409)

        scope = (context.tenant_id, context.user_id)

        if sum(streams.values()) >= max_streams or streams.get(scope, 0) >= streams_per_user:
            return JSONResponse(
                {"error": "stream_capacity"}, status_code=429, headers={"Retry-After": "1"}
            )

        streams[scope] = streams.get(scope, 0) + 1
        emit("stream.opened", context)

        async def frames():
            try:
                async for frame in stream.events():
                    yield frame
            finally:
                streams[scope] -= 1
                emit("stream.closed", context)

                if streams[scope] == 0:
                    del streams[scope]

        # Limits are per API process; fixed replica limits bound aggregate sockets.
        # Five-minute reconnects reauthenticate without marking user activity.
        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return Starlette(
        exception_handlers={
            StateNotFoundError: unavailable,
            StateConflictError: unavailable,
        },
        routes=[
            Route("/runs", admit, methods=["POST"]),
            Route("/runs/cancel", cancel, methods=["POST"]),
            Route("/events", progress),
            Route("/activity", activity, methods=["POST"]),
            Route("/history", history),
        ],
    )
