"""Authenticated HTTP/SSE boundary for an explicitly invoked operator soak run."""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2

from agent_platform.client.stream import Frame, FrameDecoder
from tests.soak.runner import Session


class CliTokens:
    """Refresh delegated credentials in memory using a selected Azure CLI profile.

    expected_user/tenant protect against accidentally exercising the operator's
    other login. These local claim checks do not authenticate a token; the API
    still verifies its signature, issuer, audience, scope and user/client allowlist.
    No login, user creation, consent or allowlist mutation happens in this helper.
    """

    def __init__(self, audience: str, tenant: str, user: str, directory: Path | None) -> None:
        self.audience = str(UUID(audience))
        self.tenant = str(UUID(tenant))
        self.user = str(UUID(user))
        self.directory = directory
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self.refreshes = 0

    async def token(self) -> str:
        async with self._lock:
            if self._expires - time.time() > 120:
                return self._token

            environment = os.environ.copy()

            if self.directory is not None:
                environment["AZURE_CONFIG_DIR"] = str(self.directory)

            process = await asyncio.create_subprocess_exec(
                "az",
                "account",
                "get-access-token",
                "--resource",
                "api://" + self.audience,
                "--tenant",
                self.tenant,
                "-o",
                "json",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
            except (TimeoutError, asyncio.CancelledError) as error:
                if process.returncode is None:
                    process.kill()

                await process.communicate()

                if isinstance(error, asyncio.CancelledError):
                    raise

                raise ValueError("delegated token refresh timed out") from None

            if process.returncode:
                raise ValueError("delegated login requires operator attention")

            result = json.loads(stdout)
            token = result["accessToken"]

            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError("invalid delegated token size")

            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))

            if (
                claims.get("tid") != self.tenant
                or claims.get("oid") != self.user
                or claims.get("idtyp") == "app"
                or "scp" not in claims
            ):
                raise ValueError("selected CLI login does not match the delegated test identity")

            self._token = token
            self._expires = float(claims["exp"])

            if self._expires - time.time() <= 120:
                raise ValueError("delegated token expires too soon")

            self.refreshes += 1
            return self._token

    def stream_seconds(self) -> float:
        """Reconnect before this request's token expires, even if other reads refreshed it."""

        return max(1, min(600, self._expires - time.time() - 60))


class HttpSoakClient:
    """Use normal endpoints, preserving stable run IDs and passive read semantics."""

    def __init__(self, client: httpx2.AsyncClient, tokens: CliTokens) -> None:
        self.client = client
        self.tokens = tokens

    async def headers(self, session: Session | None = None) -> dict[str, str]:
        headers = {"authorization": "Bearer " + await self.tokens.token()}

        if session is not None:
            headers.update({"x-conversation-id": session.conversation, "x-run-id": session.run})

        return headers

    async def create(self) -> Session:
        response = await self.client.post("/conversations", headers=await self.headers())
        response.raise_for_status()
        return Session(str(UUID(response.json()["conversation_id"])))

    async def turn(self, session: Session, text: str, expected: str | None) -> dict[str, Any]:
        started = time.monotonic()
        result: dict[str, Any] = {"status": "not_admitted", "correct": None}

        try:
            response = await self.client.post(
                "/runs",
                headers=await self.headers(session),
                json={"request_id": session.run, "text": text},
            )
            result["admission_http_status"] = response.status_code
            response.raise_for_status()
            result["admission_seconds"] = time.monotonic() - started
            deadline = time.monotonic() + 120

            while time.monotonic() < deadline:
                response = await self.client.get("/status", headers=await self.headers(session))
                response.raise_for_status()
                status = response.json()["status"]

                if status in {"completed", "failed", "cancelled", "limit_reached"}:
                    result["status"] = status
                    break

                await asyncio.sleep(0.1)
            else:
                result["status"] = "timeout"

            # /history returns the bounded recent complete-turn view, not the first
            # 100-event journal page. Scan its full tail and match the admitted run;
            # after 150 older events, a previous READY cannot satisfy this turn.
            # Missing current output fails instead of substituting an older answer.
            # Neither answer bytes nor tool/delivery records enter the evidence ledger.
            response = await self.client.get("/history", headers=await self.headers(session))
            response.raise_for_status()
            answers = [
                event["payload"]["value"]["text"]
                for event in response.json()["events"]
                if event["run_id"] == session.run
                and event["payload"]["type"] == "Message"
                and event["payload"]["value"]["role"] == "assistant"
            ]
            result["answer_count"] = len(answers)
            result["correct"] = len(answers) == 1 and (
                expected is None or answers[0].strip() == expected
            )
        except httpx2.HTTPStatusError as error:
            result["error"] = "http_failure"
            result["http_status"] = error.response.status_code
        except httpx2.TransportError:
            result["error"] = "transport_failure"

        result["turn_seconds"] = time.monotonic() - started
        return result

    async def inspect(self, session: Session) -> dict[str, Any]:
        response = await self.client.get("/inspect", headers=await self.headers(session))
        response.raise_for_status()
        source = response.json()
        cache = source["cache"]
        memory = source.get("memory", {})
        usage = source.get("usage")
        usage_fields = (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "unknown_usage",
            "expected_calls_lower_bound",
        )

        if usage is not None:
            if not isinstance(usage, dict) or any(key not in usage for key in usage_fields):
                usage = None
            elif any(type(usage[key]) is not int or usage[key] < 0 for key in usage_fields):
                raise ValueError("invalid scoped usage counters")
            else:
                usage = {key: usage[key] for key in usage_fields}

        # Pick known fields instead of retaining future /inspect additions. Numeric
        # memory publication counts may be unavailable on an older deployment; the
        # runner then leaves the repeated-compaction gate failed/unmeasured.
        return {
            "cursor": int(source["cursor"]),
            "active_run": str(UUID(source["active_run"])) if source["active_run"] else None,
            "durable": {key: int(source["durable"][key]) for key in ("runs", "journal_events")},
            "cache": {
                "available": cache.get("available") is True,
                **{
                    key: int(cache[key])
                    for key in (
                        "idle_timeout_seconds",
                        "view_ttl_ms",
                        "activity_ttl_ms",
                        "progress_ttl_ms",
                    )
                    if key in cache
                },
            },
            "summary_versions": int(memory["summary_versions"])
            if "summary_versions" in memory
            else -1,
            "token_refreshes": self.tokens.refreshes,
            "usage": usage,
        }

    async def watch(self, session: Session, observations: dict[str, Any]) -> None:
        """Consume bounded SSE frames; reconnect periodically to refresh authentication.

        A reconnect within ten minutes keeps the reader's delegated credential current.
        Its connection gaps are measured and must remain below 5% of the idle
        window. Server-generated text is discarded; only event counts survive.
        """

        cursor = None

        while True:
            headers = await self.headers(session)
            lifetime = self.tokens.stream_seconds()

            if cursor is not None:
                headers["last-event-id"] = cursor

            connected = None

            try:
                async with asyncio.timeout(lifetime):
                    async with self.client.stream(
                        "GET", "/events", headers=headers, timeout=httpx2.Timeout(30, read=None)
                    ) as response:
                        response.raise_for_status()

                        if not response.headers.get("content-type", "").startswith(
                            "text/event-stream"
                        ):
                            raise ValueError("expected SSE content type")

                        connected = time.monotonic()
                        observations["connections"] += 1
                        reauthorizations = observations.get("reauthorization_requests", 0)
                        decoder = FrameDecoder()

                        async for chunk in response.aiter_bytes():
                            observations["bytes"] = observations.get("bytes", 0) + len(chunk)

                            for offset in range(0, len(chunk), 65536):
                                for frame in decoder.feed(chunk[offset : offset + 65536]):
                                    value = observe_frame(frame, observations)

                                    if value is not None:
                                        cursor = value

                        if observations.get("reauthorization_requests", 0) > reauthorizations:
                            observations["planned_reconnects"] = (
                                observations.get("planned_reconnects", 0) + 1
                            )
                        else:
                            observations["errors"] += 1
            except TimeoutError:
                pass
            except httpx2.HTTPStatusError as error:
                observations["errors"] += 1

                if error.response.status_code in {401, 403}:
                    raise ValueError("idle stream access denied") from None
            except httpx2.HTTPError:
                observations["errors"] += 1
            finally:
                if connected is not None:
                    observations["connected_seconds"] += time.monotonic() - connected

            await asyncio.sleep(1)


def observe_frame(frame: Frame, observations: dict[str, Any]) -> str | None:
    """Count a decoded frame and validate its cursor without retaining its body."""

    cursor = frame.cursor

    if cursor is not None:
        if len(cursor) > 512:
            raise ValueError("SSE cursor exceeds bound")

        decoded = json.loads(cursor)

        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"recovery_id", "deletion_generation", "sequence"}
            or not isinstance(decoded["recovery_id"], str)
            or not 1 <= len(decoded["recovery_id"]) <= 100
            or any(
                type(decoded[key]) is not int or decoded[key] < 0
                for key in ("deletion_generation", "sequence")
            )
        ):
            raise ValueError("invalid durable SSE cursor")

        cursor = json.dumps(decoded, separators=(",", ":"))

    if frame.event == "reconnect" and frame.data.get("reason") == "reauthorize":
        observations["reauthorization_requests"] = (
            observations.get("reauthorization_requests", 0) + 1
        )

    event = (
        frame.event
        if frame.event in {"saved", "view", "text", "reset", "final", "reconnect"}
        else "other"
    )
    events = observations["events"]
    events[event] = events.get(event, 0) + 1
    return cursor
