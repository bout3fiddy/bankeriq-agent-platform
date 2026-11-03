"""Bounded SSE delivery over disposable Redis progress and durable journal cursors."""

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from agent_harness.runs.context import RunContext
from agent_harness.storage.codec import encode_items, plain
from agent_harness.storage.interface import StateConflictError
from agent_runtime.sessions.service import SessionService


@dataclass(frozen=True)
class Cursor:
    recovery_id: str
    deletion_generation: int
    sequence: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.recovery_id, str)
            or not 1 <= len(self.recovery_id) <= 100
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.deletion_generation) is not int
            or self.deletion_generation < 0
        ):
            raise ValueError("invalid durable stream cursor")

    def encode(self) -> str:
        return json.dumps(plain(self), separators=(",", ":"))


class EventBuffer:
    """Connection-local bound independent of worker and PostgreSQL transactions.

    For example, a client limited to two events that stalls on event 10 receives
    reconnect after event 12 overflows its queue. Its last delivered durable cursor
    still recovers every committed outcome. Disposable progress may be coalesced.
    """

    def __init__(self, *, max_events: int = 32, max_bytes: int = 262144) -> None:
        if not 1 <= max_events <= 256 or not 256 <= max_bytes <= 1048576:
            raise ValueError("invalid SSE buffer bounds")

        self.max_events = max_events
        self.max_bytes = max_bytes
        self.events: deque[bytes] = deque()
        self.size = 0
        self.overflow = False

    def offer(self, event: str, data: Any, cursor: Cursor | None = None) -> None:
        if self.overflow:
            return

        identifier = f"id: {cursor.encode()}\n" if cursor else ""
        frame = (identifier + f"event: {event}\ndata: " + json.dumps(data) + "\n\n").encode()

        if len(self.events) >= self.max_events or self.size + len(frame) > self.max_bytes:
            self.events.clear()
            frame = b'event: reconnect\ndata: {"reason":"slow_consumer"}\n\n'
            self.size = 0
            self.overflow = True

        self.events.append(frame)
        self.size += len(frame)

    def take(self) -> bytes | None:
        if not self.events:
            return None

        frame = self.events.popleft()
        self.size -= len(frame)
        return frame


class ProgressStream:
    """Trusted authenticated context scopes every poll; Redis never drives execution.

    Serve events() through a streaming HTTP response with media type text/event-stream.
    Disconnect cancels only this reader. The durable cursor identifies journal progress,
    while run revision and worker attempt identify disposable status generations.
    """

    def __init__(
        self,
        sessions: SessionService,
        context: RunContext,
        *,
        cursor: Cursor | None = None,
        max_events: int = 32,
        max_bytes: int = 262144,
        poll_seconds: float = 0.25,
    ) -> None:
        if not 0.01 <= poll_seconds <= 10:
            raise ValueError("invalid SSE polling interval")

        self.sessions = sessions
        self.context = context
        self.cursor = cursor
        self.buffer = EventBuffer(max_events=max_events, max_bytes=max_bytes)
        self.poll_seconds = poll_seconds
        self._last_progress: str | None = None

    def poll(self) -> None:

        # Read one bounded page per tick. Never hold a transaction while waiting
        # for the HTTP socket; all committed state is copied before queueing bytes.
        after = self.cursor.sequence if self.cursor else 0
        page = self.sessions.store.history_page(self.context, after=after, limit=1)

        if self.cursor and (
            self.cursor.recovery_id != page.recovery_id
            or self.cursor.deletion_generation != page.deletion_generation
        ):
            raise StateConflictError("stream cursor belongs to another generation")

        if page.next_cursor > after:
            self.cursor = Cursor(page.recovery_id, page.deletion_generation, page.next_cursor)
            self.buffer.offer("saved", {"items": encode_items(page.items)}, self.cursor)

        # Ordinary progress reads only metadata and its separate bounded Redis
        # key. History is fetched solely by the explicit durable cursor above.
        progress = self.sessions.progress_view(self.context)
        encoded = json.dumps(progress, sort_keys=True)

        if encoded != self._last_progress:
            self.buffer.offer("view", progress)
            self._last_progress = encoded

    async def events(self) -> AsyncGenerator[bytes]:
        available = asyncio.Event()

        async def produce() -> None:
            while not self.buffer.overflow:
                await asyncio.to_thread(self.poll)
                available.set()
                await asyncio.sleep(self.poll_seconds)

        producer = asyncio.create_task(produce())

        try:
            while True:
                if producer.done():
                    producer.result()

                frame = self.buffer.take()

                if frame is not None:
                    yield frame
                elif self.buffer.overflow:
                    return
                else:
                    available.clear()

                    with suppress(TimeoutError):
                        await asyncio.wait_for(available.wait(), self.poll_seconds)
        finally:
            producer.cancel()

            with suppress(asyncio.CancelledError):
                await producer
