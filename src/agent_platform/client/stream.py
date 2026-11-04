"""Bounded SSE framing and idempotent presentation for one owned run."""

import json
from dataclasses import dataclass
from typing import Any


class StreamProtocolError(ValueError):
    """The peer supplied invalid or oversized presentation data."""


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict[str, Any]
    cursor: str | None = None


class FrameDecoder:
    """Decode fragmented UTF-8/SSE frames without an unbounded line buffer.

    A chunk may end halfway through a Unicode character or contain several events.
    Keep bytes until a complete frame arrives, then decode and validate its JSON.
    Comments/heartbeats have no presentation meaning. A reconnect cursor is retained
    only after the caller accepts its frame; receiving bytes alone advances nothing.
    """

    def __init__(self, max_bytes: int = 262144) -> None:
        self.max_bytes = max_bytes
        self.pending = bytearray()

    def feed(self, raw: bytes) -> list[Frame]:
        if len(raw) > self.max_bytes:
            raise StreamProtocolError("SSE transport chunk exceeds its bound")

        frames = []

        # Iterate bounded pieces so a large transport read containing many small
        # valid events cannot force the persistent parser buffer above its limit.
        for byte in raw:
            self.pending.append(byte)

            if len(self.pending) > self.max_bytes:
                raise StreamProtocolError("SSE frame exceeds its bound")

            if self.pending.endswith(b"\n\n") or self.pending.endswith(b"\r\n\r\n"):
                frames.extend(self._frame(bytes(self.pending)))
                self.pending.clear()

        return frames

    @staticmethod
    def _frame(raw: bytes) -> list[Frame]:
        event, cursor = "message", None
        parts = []

        try:
            for line in raw.decode("utf-8").splitlines():
                field, separator, value = line.partition(":")

                if not separator or not field:
                    continue

                value = value.removeprefix(" ")

                if field == "event":
                    event = value
                elif field == "id":
                    if len(value) > 512 or "\x00" in value:
                        raise StreamProtocolError("invalid SSE cursor")

                    cursor = value
                elif field == "data":
                    parts.append(value)

            if not parts:
                return []

            data = json.loads("\n".join(parts))

            if not isinstance(data, dict):
                raise StreamProtocolError("SSE event must be an object")

            return [Frame(event, data, cursor)]
        except UnicodeError, json.JSONDecodeError:
            raise StreamProtocolError("invalid SSE encoding") from None


class RunPresentation:
    """Replace provisional snapshots and accept one durable final answer per run.

    If attempt 4 supersedes attempt 3, a delayed attempt-3 frame is ignored. A
    cumulative snapshot can repair a missing sequence without replaying fragments.
    The final event always replaces the draft. Reconnecting may repeat that final
    event; this state emits the completed presentation only once.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.text = ""
        self.generation = (0, 0)
        self.sequence = 0
        self.final: dict[str, Any] | None = None
        self.cursor: str | None = None
        self.gaps = 0

    def apply(self, frame: Frame) -> bool:

        # Saved events carry a conversation cursor, independent of the active run.
        # Text/final frames from another run must not move that reconnect position.
        if frame.event == "saved" and frame.cursor is not None:
            self.cursor = frame.cursor
            return False

        data = frame.data

        if data.get("run_id") != self.run_id or self.final is not None:
            return False

        if frame.event == "final":
            answer = data.get("answer")

            if (
                data.get("status") not in {"completed", "failed", "cancelled", "limit_reached"}
                or data.get("provisional") is not False
                or (answer is not None and not isinstance(answer, str))
                or (isinstance(answer, str) and len(answer.encode()) > 131072)
                or (data.get("status") == "completed" and not answer)
            ):
                raise StreamProtocolError("invalid final answer")

            self.text = answer or ""
            self.final = data
            return True

        if frame.event not in {"text", "reset"}:
            return False

        if frame.event == "reset" and "step" not in data:
            self.text = ""
            self.gaps += 1
            return True

        try:
            values = [data[key] for key in ("step", "attempt", "sequence")]

            if any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdigit()
                or not 1 <= len(value) <= 20
                for value in values
            ):
                raise ValueError

            step, attempt, sequence = (int(value) for value in values)

            if min(step, attempt, sequence) < 1:
                raise ValueError

            generation = (step, attempt)
        except KeyError, TypeError, ValueError:
            raise StreamProtocolError("invalid text generation") from None

        if generation < self.generation or (
            generation == self.generation and sequence <= self.sequence
        ):
            return False

        text = data.get("text")

        if (
            not isinstance(text, str)
            or len(text.encode()) > 32768
            or data.get("provisional") is not True
        ):
            raise StreamProtocolError("invalid provisional text")

        if generation == self.generation and sequence > self.sequence + 1:
            self.gaps += 1

        self.generation, self.sequence = generation, sequence
        self.text = text if frame.event == "text" else ""
        return True
