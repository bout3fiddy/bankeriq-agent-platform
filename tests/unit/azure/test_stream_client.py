"""Client presentation tolerates reconnect duplicates without duplicating an answer."""

import asyncio
import json

import httpx2
import pytest

from agent_platform.client.stream import Frame, FrameDecoder, RunPresentation, StreamProtocolError
from agent_platform.client.transport import receive_run


def text(value, *, step=1, attempt=1, sequence=1):
    return Frame(
        "text",
        {
            "run_id": "run",
            "text": value,
            "step": str(step),
            "attempt": str(attempt),
            "sequence": str(sequence),
            "provisional": True,
        },
    )


def final():
    return Frame(
        "final",
        {
            "run_id": "run",
            "answer": "Final answer",
            "status": "completed",
            "provisional": False,
        },
    )


def wire(frame):
    return (
        f"event: {frame.event}\ndata: " + json.dumps(frame.data, ensure_ascii=False) + "\n\n"
    ).encode()


def test_fragmented_unicode_and_crlf_are_decoded_after_complete_frames():
    decoder = FrameDecoder()
    body = wire(text("€ vijf")).replace(b"\n", b"\r\n")
    frames = []

    for byte in body:
        frames.extend(decoder.feed(bytes([byte])))

    assert frames == [text("€ vijf")]
    assert decoder.pending == b""


def test_new_attempt_and_final_replace_old_text_without_duplicates():
    state = RunPresentation("run")
    assert state.apply(text("old"))
    assert state.apply(text("new", attempt=2))
    assert not state.apply(text("late old", sequence=99))
    assert state.text == "new"
    assert state.apply(text("new snapshot", attempt=2, sequence=5))
    assert state.gaps == 1
    assert state.apply(final())
    assert not state.apply(final())
    assert not state.apply(text("stale", step=2))
    assert state.text == "Final answer"


def test_reset_and_cross_run_data_do_not_create_a_completed_answer():
    state = RunPresentation("run")
    state.apply(text("partial"))
    assert state.apply(Frame("reset", {"run_id": "run", "reason": "progress_unavailable"}))
    assert state.text == "" and state.final is None
    assert not state.apply(Frame("final", {**final().data, "run_id": "other"}))
    assert not state.apply(Frame("final", {**final().data, "run_id": "other"}, "foreign"))
    assert state.cursor is None
    assert not state.apply(Frame("saved", {"items": []}, "durable"))
    assert state.cursor == "durable"


@pytest.mark.parametrize("raw", [b"x" * 257, b"data: []\n\n", b"data: invalid\n\n", b"\xff\n\n"])
def test_malformed_and_oversized_frames_fail_explicitly(raw):
    with pytest.raises(StreamProtocolError):
        FrameDecoder(max_bytes=256).feed(raw)


def test_transport_reconnects_without_posting_or_repeating_final():
    requests, updates = [], []

    def respond(request):
        requests.append(request.method)
        frames = [text("partial"), Frame("reconnect", {"reason": "slow_consumer"})]

        if len(requests) > 1:
            frames = [Frame("reset", {"run_id": "run"}), final(), final()]

        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(wire(frame) for frame in frames),
        )

    async def run():
        async def headers():
            return {"authorization": "Bearer synthetic"}

        async with httpx2.AsyncClient(
            base_url="https://synthetic.invalid", transport=httpx2.MockTransport(respond)
        ) as client:
            return await receive_run(
                client,
                RunPresentation("run"),
                headers=headers,
                on_update=lambda state: updates.append((state.text, state.final is not None)),
            )

    assert asyncio.run(run()).final == final().data
    assert requests == ["GET", "GET"]
    assert updates == [("partial", False), ("", False), ("Final answer", True)]
