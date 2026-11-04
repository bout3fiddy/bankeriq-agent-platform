"""Private test channels rotate between calls without replaying uncertain requests."""

import asyncio
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
import pytest

from tests.azure import exec_transport


class SyntheticTransport(exec_transport.ExecTransport):
    def __init__(self):
        super().__init__(None, "synthetic")
        self.opens = 0
        self.sent = []
        self.lose_response = False
        self.closed_helpers = 0

    async def connect(self):
        self.opens += 1
        self.socket = object()
        self.connected_at = exec_transport.monotonic()

    async def send(self, value):
        self.sent.append(value)

    async def until(self, marker):
        if marker == "BYE\n":
            assert self.sent[-1] == b"QUIT\n" and self.buffer == ""
            self.closed_helpers += 1
            self.buffer = "BYE\n"
            return

        if self.lose_response:
            raise RuntimeError("synthetic disconnected channel")

        response = {"status": 200, "body": {"usage": {"input_tokens": 3}}, "headers": {}}
        self.buffer = "RESULT=" + base64.b64encode(json.dumps(response).encode()).decode()


def test_completed_requests_rotate_before_the_next_dispatch(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(exec_transport, "monotonic", lambda: now[0])

    async def scenario():
        transport = SyntheticTransport()
        request = httpx2.Request("POST", "https://synthetic/chat/completions", json={})
        assert (await transport.handle_async_request(request)).status_code == 200
        now[0] = 119
        await transport.handle_async_request(request)
        assert transport.opens == 1
        now[0] = 120
        await transport.handle_async_request(request)
        assert transport.opens == 2
        assert len(transport.sent) == 4
        assert transport.sent[2] == b"QUIT\n"
        await transport.aclose()
        assert transport.socket is None
        assert transport.closed_helpers == 2

    asyncio.run(scenario())


def test_lost_response_is_never_replayed_even_after_rotation_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(exec_transport, "monotonic", lambda: now[0])

    async def scenario():
        transport = SyntheticTransport()
        transport.lose_response = True
        request = httpx2.Request("POST", "https://synthetic/chat/completions", json={})

        with pytest.raises(RuntimeError, match="disconnected"):
            await transport.handle_async_request(request)

        now[0] = 121

        with pytest.raises(RuntimeError, match="reconciliation"):
            await transport.handle_async_request(request)

        assert transport.opens == 1 and len(transport.sent) == 1
        await transport.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("include_remote_timing", [False, True])
def test_local_relay_preserves_response_and_reports_only_received_timing(include_remote_timing):
    async def scenario(path):
        observed = []
        body = {"id": "synthetic-response", "usage": {"input_tokens": 3}}
        headers = (
            {
                "x-test-provider-seconds": "1.25",
                "x-test-channel-setup-seconds": "9.5",
                "x-test-channel-opened": "1",
            }
            if include_remote_timing
            else {}
        )

        async def respond(reader, writer):
            size = int.from_bytes(await reader.readexactly(4))
            assert json.loads(await reader.readexactly(size)) == {"model": "synthetic"}
            value = json.dumps({"status": 200, "body": body, "headers": headers}).encode()
            writer.write(len(value).to_bytes(4) + value)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(respond, path=path)

        async with server:
            transport = exec_transport.LocalRelayTransport(path, observer=observed.append)
            response = await transport.handle_async_request(
                httpx2.Request(
                    "POST", "https://synthetic/chat/completions", json={"model": "synthetic"}
                )
            )

        assert response.json() == body
        assert observed == [
            {
                "provider_seconds": 1.25 if include_remote_timing else None,
                "channel_setup_seconds": 9.5 if include_remote_timing else None,
                "channel_opened": include_remote_timing,
            }
        ]

    # macOS limits Unix socket paths below pytest's deeply nested temporary path.
    with TemporaryDirectory(prefix="relay-", dir="/tmp") as directory:
        asyncio.run(scenario(Path(directory) / "relay.sock"))
