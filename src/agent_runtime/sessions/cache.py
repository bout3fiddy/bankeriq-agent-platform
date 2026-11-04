"""Redis connections, atomic cursor fences and bounded provisional display state."""

import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

from agent_harness.storage.codec import fingerprint
from agent_harness.storage.interface import StateConflictError


class RedisSessions:
    """Application-owned client; errors become misses without exposing connection details."""

    def __init__(self, url: str, *, ttl_seconds: int = 1800, client: Redis | None = None) -> None:
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("session TTL must be between 1 and 86400 seconds")

        self.ttl_seconds = ttl_seconds
        self.client = client or Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        self._script = self.client.register_script(
            Path(__file__).with_name("session.lua").read_text()
        )

        self._progress_script = self.client.register_script(
            Path(__file__).with_name("progress.lua").read_text()
        )
        self._activity_script = self.client.register_script(
            Path(__file__).with_name("activity.lua").read_text()
        )
        self._text_script = self.client.register_script(
            Path(__file__).with_name("text.lua").read_text()
        )

    def text(
        self, head: dict[str, Any], run_id: str, value: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Keep one bounded text snapshot for this run, independent of history/progress.

        A reader missing sequences 3–9 can replace its draft with snapshot 10.
        The payload is disposable and expires without SSE reads renewing its TTL.
        Primary lease checks at publication and delivery decide whether it is current.
        """

        encoded = json.dumps(value, ensure_ascii=False)

        if len(encoded.encode()) > 40000:
            raise ValueError("provisional text exceeds its bound")

        key = self.key(head) + f":text:{head['deletion_generation']}:{fingerprint(run_id)}"

        try:
            raw = self._text_script(
                keys=[key],
                args=["write" if value is not None else "read", encoded, self.ttl_seconds],
            )
            raw = cast(bytes | str | None, raw)
            value = json.loads(raw) if raw and len(raw) <= 40000 else None

            if not isinstance(value, dict):
                return None

            if (
                value.get("kind") not in {"text", "reset"}
                or not isinstance(value.get("text"), str)
                or len(value["text"].encode()) > 32768
                or value.get("provisional") is not True
                or any(
                    not isinstance(value.get(field), str)
                    or not value[field].isascii()
                    or not value[field].isdigit()
                    or not 1 <= len(value[field]) <= 20
                    for field in ("step", "attempt", "sequence")
                )
            ):
                return None

            return value
        except RedisError, ValueError, TypeError:
            return None

    def touch(self, head: dict[str, Any], *, minimum_interval: int = 0) -> bool:
        """Extend retention after explicit interaction or an observed live SQL lease.

        Example: a tool still holds a renewed lease after 30 minutes. Maintenance
        supplies its current conversation head; existing history/progress keys get
        another TTL without copying history. Missing payloads remain cache misses.
        The activity marker shares the recovery/deletion scope and expires too, so
        a dead maintenance process cannot leave permanent Redis reservations.
        """

        key = self.key(head)

        with suppress(RedisError):
            return bool(
                self._activity_script(
                    keys=[
                        key,
                        key + f":progress:{head['deletion_generation']}",
                        key + f":activity:{head['deletion_generation']}",
                    ],
                    args=[self.ttl_seconds, minimum_interval],
                )
            )

        return False

    def inspect(self, head: dict[str, Any]) -> dict[str, int | bool]:
        key = self.key(head)

        try:
            with self.client.pipeline(transaction=False) as pipeline:
                pipeline.pttl(key)
                pipeline.pttl(key + f":progress:{head['deletion_generation']}")
                pipeline.pttl(key + f":activity:{head['deletion_generation']}")
                view, progress, activity = pipeline.execute()

            return {
                "available": True,
                "idle_timeout_seconds": self.ttl_seconds,
                "view_ttl_ms": view,
                "progress_ttl_ms": progress,
                "activity_ttl_ms": activity,
            }
        except RedisError:
            return {"available": False}

    def progress(self, head: dict[str, Any], **values: Any) -> list[dict[str, Any]]:
        """Read/write only the bounded progress document, scoped to this generation.

        A restored database uses a different base key; deletion also changes this
        suffix. An old producer can leave an expiring disposable key, but a current
        reader never serves it. No transcript is fetched or serialized here.
        """

        try:
            raw = self._progress_script(
                keys=[self.key(head) + f":progress:{head['deletion_generation']}"],
                args=["write" if values else "read", json.dumps(values), self.ttl_seconds],
            )
            return (
                [json.loads(item) for item in json.loads(cast(bytes | str, raw))["progress"]]
                if raw
                else []
            )
        except RedisError, ValueError, TypeError, KeyError:
            return []

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def key(head: dict[str, Any]) -> str:

        # Hash the scope as a tuple: tenant="a:b", chat="c" cannot collide with
        # tenant="a", chat="b:c". A restored primary gets a new namespace, so a
        # high cursor from its old timeline cannot defeat the new lower cursor.
        scope = fingerprint([head["tenant_id"], head["conversation_id"], head["user_id"]])
        return f"bankeriq:session:v1:{head['recovery_id']}:{scope}"

    def apply(self, op: str, head: dict[str, Any], **values: Any) -> dict[str, Any] | None:
        arguments = {
            "generation": str(head["deletion_generation"]),
            "cursor": str(head["cursor"]),
            "last_activity_age": head.get("idle_seconds", 0),
            **values,
        }

        try:
            result = self._script(
                keys=[
                    self.key(head),
                    self.key(head) + f":activity:{head['deletion_generation']}",
                ],
                args=[op, json.dumps(arguments), str(uuid4()), self.ttl_seconds],
            )
        except RedisError:
            return None

        if result == -2:
            raise StateConflictError("provisional request ID reused with different input")

        if result == -3:
            raise StateConflictError("provisional display capacity exceeded")

        if not result or isinstance(result, int):
            return {"removed": True} if result == 1 else None

        result = cast(bytes | str, result)

        try:
            if len(result) > 2097152:
                return None

            return json.loads(result)
        except TypeError, ValueError:
            return None

    def acquire_refill(self, head: dict[str, Any]) -> str | None:
        token = str(uuid4())

        try:
            acquired = self.client.set(self.key(head) + ":refill", token, nx=True, px=5000)
            return token if acquired else None
        except RedisError:
            return None

    def release_refill(self, head: dict[str, Any], token: str) -> None:
        with suppress(RedisError):
            self.client.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                "return redis.call('DEL', KEYS[1]) end return 0",
                1,
                self.key(head) + ":refill",
                token,
            )
