"""Payload-free measurements delivered to an application-owned observation callback."""

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

from openai.types.chat import ChatCompletion


@dataclass(frozen=True)
class ModelObservation:
    elapsed_ms: int
    status_code: int
    summary: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_limit: int | None = None
    tokens_remaining: int | None = None


def observe(
    callback: Callable[[ModelObservation], None] | None,
    started: float,
    status: int,
    summary: bool,
    headers: Mapping[str, str],
    response: ChatCompletion | None = None,
) -> None:
    """Export only typed numeric metadata; callback failure cannot repeat inference.

    For example, observe=records.append receives input_tokens=132 and elapsed_ms=850.
    No request, reply text, provider error, authorization header or request ID is
    accepted by the callback. Missing usage remains None, never an invented zero.
    """

    if callback is None:
        return

    def number(name):
        value = headers.get(name, "")
        return int(value) if value.isascii() and value.isdigit() and len(value) <= 10 else None

    usage = response.usage if response else None
    record = ModelObservation(
        round((monotonic() - started) * 1000),
        status,
        summary,
        usage.prompt_tokens if usage else None,
        usage.completion_tokens if usage else None,
        number("x-ratelimit-limit-tokens"),
        number("x-ratelimit-remaining-tokens"),
    )

    with suppress(Exception):
        callback(record)
