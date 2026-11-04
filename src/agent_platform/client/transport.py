"""Async SSE consumption with finite reconnects and application-owned credentials."""

import asyncio
from collections.abc import Awaitable, Callable

import httpx2

from agent_platform.client.stream import FrameDecoder, RunPresentation


async def receive_run(
    client: httpx2.AsyncClient,
    state: RunPresentation,
    *,
    headers: Callable[[], Awaitable[dict[str, str]]],
    on_update: Callable[[RunPresentation], None],
    timeout: float = 30,
) -> RunPresentation:
    """Reconnect with the same run/cursor; receiving never resubmits accepted input.

    headers() refreshes authentication outside this module. A missing Redis draft
    produces a reset, while PostgreSQL supplies the final. The callback receives
    presentation state only; it cannot change worker ownership or tool authority.
    A timed-out caller retains its run ID and can invoke receive_run again later.
    """

    if not 0 < timeout <= 900:
        raise ValueError("invalid client wait bound")

    async with asyncio.timeout(timeout):
        for reconnect in range(6):
            request_headers = await headers()

            if state.cursor is not None:
                request_headers["Last-Event-ID"] = state.cursor

            decoder = FrameDecoder()

            try:
                async with client.stream("GET", "/events", headers=request_headers) as response:
                    response.raise_for_status()

                    async for data in response.aiter_bytes():
                        for frame in decoder.feed(data):
                            if frame.event == "reconnect":
                                break

                            if state.apply(frame):
                                on_update(state)

                            if state.final is not None:
                                return state
                        else:
                            continue

                        break
            except httpx2.TransportError:
                pass

            await asyncio.sleep(min(1, 0.1 * 2**reconnect))

    raise ConnectionError("stream reconnect budget exhausted; resume the same run")
