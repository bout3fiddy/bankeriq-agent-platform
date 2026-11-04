"""Validation-only relay to private Foundry through one existing coordinator exec channel.

No files or app configuration change inside Azure. Only synthetic requests and responses
cross this operator-authenticated connection; managed identity tokens stay in the container.
The local Unix socket is owner-only, bounded, and removed when the test relay shuts down.
Production runtime code has no dependency on this transport.
"""

import argparse
import asyncio
import base64
import contextlib
import json
import signal
import zlib
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode

import aiohttp
import httpx2
from azure.identity.aio import AzureCliCredential

# Raw reads avoid interactive input() line editing. One SDK client lives only for
# this validation connection; each completion receives the full explicit request.
# The alarm bounds helper lifetime if its operator loses the connection.
HELPER = r"""import base64,json,os,signal,time,tty,zlib
from azure.identity import ManagedIdentityCredential,get_bearer_token_provider
from openai import OpenAI, APIStatusError
signal.alarm(1200)
tty.setraw(0)
with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential:
    token=get_bearer_token_provider(credential,"https://cognitiveservices.azure.com/.default")
    with OpenAI(base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],api_key=token,
                max_retries=0,timeout=60) as client:
        print("READY",flush=True)
        while True:
            raw=b""
            while not raw.endswith(b"\n"):
                chunk=os.read(0,4096)
                if not chunk or len(raw)+len(chunk)>65536:
                    raise SystemExit
                raw+=chunk
            if raw.strip()==b"QUIT":
                break
            try:
                request=json.loads(zlib.decompress(base64.b64decode(raw)))
                started=time.perf_counter()
                response=client.chat.completions.with_raw_response.create(**request)
                result={"status":response.status_code,"body":response.parse().model_dump(),
                        "headers":{k:v for k,v in response.headers.items()
                                   if k.startswith("x-ratelimit-") or k.startswith("retry-after")}}
                result["headers"]["x-test-provider-seconds"]=str(time.perf_counter()-started)
                print("RESULT="+base64.b64encode(json.dumps(result).encode()).decode(),flush=True)
            except APIStatusError as error:
                result={"status":error.status_code,
                        "body":{"error":{"code":error.code,"message":"Provider rejected request"}},
                        "headers":{k:v for k,v in error.response.headers.items()
                                   if k.startswith("x-ratelimit-") or k.startswith("retry-after")}}
                result["headers"]["x-test-provider-seconds"]=str(time.perf_counter()-started)
                print("RESULT="+base64.b64encode(json.dumps(result).encode()).decode(),flush=True)
            except Exception as error:
                print("FAILURE="+type(error).__name__,flush=True)
print("BYE",flush=True)
raise SystemExit
"""


class ExecTransport(httpx2.AsyncBaseTransport):
    def __init__(self, credential, resource_id, *, lifetime_seconds=1200):
        if lifetime_seconds not in (1200, 3600):
            raise ValueError("relay lifetime must be twenty minutes or one hour")

        self.credential = credential
        self.resource_id = resource_id
        self.lifetime_seconds = lifetime_seconds
        self.stack = AsyncExitStack()
        self.socket = None
        self.buffer = ""
        self.lock = asyncio.Lock()
        self.connected_at = 0.0
        self.failed = False
        self.phase = "idle"

    async def connect(self):
        token = await self.credential.get_token("https://management.azure.com/.default")
        root = "https://management.azure.com" + self.resource_id
        version = {"api-version": "2025-07-01"}
        client = await self.stack.enter_async_context(
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        )
        headers = {"Authorization": "Bearer " + token.token}

        async with client.get(root, params=version, headers=headers) as response:
            response.raise_for_status()
            app = await response.json()

        revision = app["properties"]["latestReadyRevisionName"]

        async with client.get(
            root + "/revisions/" + revision + "/replicas", params=version, headers=headers
        ) as response:
            response.raise_for_status()
            replicas = await response.json()

        container = replicas["value"][0]["properties"]["containers"][0]

        async with client.post(
            root + "/getAuthToken", params=version, headers=headers, json={}
        ) as response:
            response.raise_for_status()
            access = await response.json()

        # Bootstrap on stdin because the exec service parses command arguments.
        # Request text never becomes executable code or a shell argument.
        endpoint = container["execEndpoint"] + "?" + urlencode({"command": "python -u -q"})
        self.socket = await self.stack.enter_async_context(
            client.ws_connect(
                endpoint, headers={"Authorization": "Bearer " + access["properties"]["token"]}
            )
        )
        await self.socket.send_str('\x00\x04{"Width":200,"Height":50}')
        await self.until(">>> ")
        helper = HELPER.replace("signal.alarm(1200)", f"signal.alarm({self.lifetime_seconds})")
        bootstrap = (
            b'exec(__import__("base64").b64decode("' + base64.b64encode(helper.encode()) + b'"))\n'
        )
        await self.send(bootstrap)
        await self.until("READY\n")
        self.buffer = ""
        self.connected_at = monotonic()
        print("private inference channel opened", datetime.now(UTC).isoformat(), flush=True)

    async def send(self, value):
        assert self.socket is not None

        # Small frames work for requests above the exec terminal's 4 KiB boundary.
        for offset in range(0, len(value), 512):
            await self.socket.send_str("\x00\x00" + value[offset : offset + 512].decode())
            await asyncio.sleep(0.01)

    async def until(self, marker):
        assert self.socket is not None

        async with asyncio.timeout(100):
            while marker not in self.buffer:
                message = await self.socket.receive()

                if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    raise RuntimeError("private inference relay closed")

                if message.type != aiohttp.WSMsgType.BINARY:
                    continue

                frame = message.data

                if frame[:2] in (b"\x00\x01", b"\x00\x02"):
                    self.buffer += frame[2:].decode()

                if len(self.buffer) > 262144:
                    raise ValueError("private inference response exceeds test bound")

    async def handle_async_request(self, request):
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            raise ValueError("exec relay only accepts a chat completion")

        async with self.lock:
            if self.failed:
                raise RuntimeError("private inference relay requires usage reconciliation")

            setup_started = monotonic()
            opened_channel = self.socket is None

            # The operator exec channel can expire during a long quality suite.
            # Rotate after two minutes, only between fully received responses.
            # A request that loses its response is never replayed: its caller
            # retains the usage reservation and must reconcile before a new run.
            if self.socket is not None and monotonic() - self.connected_at >= 120:
                opened_channel = True
                await self.aclose()

            if self.socket is None:
                self.phase = "connect"
                await self.connect()

            setup_seconds = monotonic() - setup_started if opened_channel else 0.0
            body = await request.aread()
            payload = base64.b64encode(zlib.compress(body)) + b"\n"
            self.buffer = ""

            try:
                self.phase = "send"
                await self.send(payload)
                self.phase = "receive"
                await self.until("\n")
            except Exception:
                self.failed = True
                raise

            line = self.buffer.strip()

            if line.startswith("RESULT="):
                result = json.loads(base64.b64decode(line.removeprefix("RESULT=")))

                # Keep operator-channel setup separate from the remote SDK call.
                # For example, a nine-second reconnect plus a one-second response
                # still retains its ten-second end-to-end measurement. These test
                # headers add measured boundaries; they never subtract an estimate.
                result["headers"]["x-test-channel-setup-seconds"] = str(setup_seconds)
                result["headers"]["x-test-channel-opened"] = str(int(opened_channel))
                return httpx2.Response(
                    result["status"],
                    json=result["body"],
                    headers=result["headers"],
                    request=request,
                )

            reason = line if line.startswith("FAILURE=") else "invalid response"
            raise RuntimeError("private inference helper failed: " + reason)

    async def aclose(self):
        if self.socket is not None:
            with contextlib.suppress(Exception):
                self.buffer = ""
                await self.send(b"QUIT\n")

                # Let the helper acknowledge client/credential closure before
                # dropping its terminal. Immediate disconnect can strand an exec
                # process until its alarm. A broken channel gets at most five
                # seconds here; its unknown provider usage remains unresolved.
                async with asyncio.timeout(5):
                    await self.until("BYE\n")

                print("private inference channel closed", datetime.now(UTC).isoformat(), flush=True)

        await self.stack.aclose()
        self.stack = AsyncExitStack()
        self.socket = None


class LocalRelayTransport(httpx2.AsyncBaseTransport):
    def __init__(self, socket_path, *, observer=None):
        self.socket_path = socket_path
        self.observer = observer

    async def handle_async_request(self, request):
        body = await request.aread()

        if len(body) > 262144 or not request.url.path.endswith("/chat/completions"):
            raise ValueError("invalid local inference request")

        reader, writer = await asyncio.open_unix_connection(self.socket_path)

        try:
            writer.write(len(body).to_bytes(4) + body)
            await writer.drain()
            size = int.from_bytes(await reader.readexactly(4))

            if size > 262144:
                raise ValueError("local inference response exceeds bound")

            response = json.loads(await reader.readexactly(size))
            headers = response.get("headers", {})

            # Each callback belongs to this exact received response. Older helpers
            # have no remote timing, represented as None rather than reconstructed
            # from the total. Only numeric metadata crosses this optional boundary.
            if self.observer is not None:
                self.observer(
                    {
                        "provider_seconds": float(headers["x-test-provider-seconds"])
                        if "x-test-provider-seconds" in headers
                        else None,
                        "channel_setup_seconds": float(headers["x-test-channel-setup-seconds"])
                        if "x-test-channel-setup-seconds" in headers
                        else None,
                        "channel_opened": headers.get("x-test-channel-opened") == "1",
                    }
                )

            return httpx2.Response(
                response["status"],
                json=response["body"],
                headers=headers,
                request=request,
            )
        finally:
            writer.close()
            await writer.wait_closed()


async def serve(path: Path, resource_id: str, *, lifetime_seconds: int = 1200):
    if path.exists():
        raise ValueError("relay socket already exists")

    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)

    async with AzureCliCredential() as credential:
        transport = ExecTransport(credential, resource_id, lifetime_seconds=lifetime_seconds)

        async def handle(reader, writer):
            try:
                async with asyncio.timeout(110):
                    size = int.from_bytes(await reader.readexactly(4))

                    if size > 262144:
                        raise ValueError("local inference request exceeds bound")

                    body = await reader.readexactly(size)
                    response = await transport.handle_async_request(
                        httpx2.Request(
                            "POST", "https://relay.invalid/chat/completions", content=body
                        )
                    )
                    result = {
                        "status": response.status_code,
                        "body": response.json(),
                        "headers": dict(response.headers),
                    }
            except Exception as error:
                print(
                    "relay request failed:",
                    str(error) if isinstance(error, RuntimeError) else type(error).__name__,
                    "phase:",
                    transport.phase,
                    flush=True,
                )
                result = {"status": 503, "body": {"error": type(error).__name__}}

            try:
                value = json.dumps(result).encode()
                writer.write(len(value).to_bytes(4) + value)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            await transport.connect()
            server = await asyncio.start_unix_server(handle, path=path)
            path.chmod(0o600)
            print("private inference relay ready", flush=True)

            async with server:
                with contextlib.suppress(TimeoutError):

                    # Paced quality suites can exceed twenty minutes. The explicit
                    # one-hour option still expires locally before the helper's
                    # alarm; SIGTERM/normal shutdown sends QUIT immediately.
                    await asyncio.wait_for(stop.wait(), lifetime_seconds - 50)
        finally:
            await transport.aclose()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--lifetime-seconds", type=int, choices=[1200, 3600], default=1200)
    arguments = parser.parse_args()
    asyncio.run(
        serve(
            arguments.socket,
            arguments.resource_id,
            lifetime_seconds=arguments.lifetime_seconds,
        )
    )
