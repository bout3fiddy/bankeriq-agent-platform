"""Small authenticated admission/SSE transport; no execution lives in HTTP requests."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent_runtime.runs.context import RunContext
from agent_runtime.storage.interface import StateConflictError, StateNotFoundError
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.sessions.stream import Cursor, ProgressStream
from bankeriq_agent_platform.telemetry import emit

from .coordinator import QueuedCoordinator


class Admission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=32768)


def create_async_app(
    runtime: QueuedCoordinator,
    sessions: SessionService,
    *,
    authenticate: Callable[[Request], Awaitable[RunContext]],
    prepare_input: Callable[[RunContext, str], str],
) -> Starlette:
    """Trusted authentication supplies the complete scoped context for each request.

    For example, authenticate(request) verifies a session and resolves its owned
    conversation/run from a route token. JSON cannot choose tenant or user identity.
    prepare_input performs application input policy before the accepted journal write.
    Enterprise authentication and the browser application remain separate integrations.
    """

    async def admit(request: Request) -> Response:
        context = await authenticate(request)
        body = bytearray()

        async for chunk in request.stream():
            body.extend(chunk)

            if len(body) > 65536:
                return JSONResponse({"error": "input_too_large"}, status_code=413)

        try:
            admission = Admission.model_validate_json(body)
            text = await asyncio.to_thread(prepare_input, context, admission.text)
            run_id = await runtime.admit(context, request_id=admission.request_id, text=text)
        except ValidationError, ValueError:
            emit("admission.rejected", context)
            return JSONResponse({"error": "invalid_input"}, status_code=400)
        except StateConflictError:
            return JSONResponse({"error": "conflict"}, status_code=409)

        # Acceptance already committed. Cache/logging work cannot revoke that
        # receipt; a reconnect can recover the same request if Redis is down.
        emit("admission.accepted", context)

        with suppress(Exception):
            await asyncio.to_thread(sessions.record_activity, context)

        return JSONResponse({"accepted": True, "run_id": run_id}, status_code=202)

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

        return StreamingResponse(
            stream.events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    return Starlette(
        exception_handlers={
            StateNotFoundError: unavailable,
            StateConflictError: unavailable,
        },
        routes=[
            Route("/runs", admit, methods=["POST"]),
            Route("/events", progress),
            Route("/activity", activity, methods=["POST"]),
            Route("/history", history),
        ],
    )
