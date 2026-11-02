"""Redis connections, atomic cursor fences and bounded provisional display state."""

import json
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

from agent_runtime.storage.interface import StateConflictError
from bankeriq_agent_platform.storage.codec import fingerprint


class RedisSessions:
    """Application-owned client; errors become misses without exposing connection details."""

    def __init__(self, url: str, *, ttl_seconds: int = 1800) -> None:
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("session TTL must be between 1 and 86400 seconds")

        self.ttl_seconds = ttl_seconds
        self.client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        self._script = self.client.register_script(
            Path(__file__).with_name("session.lua").read_text()
        )

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
                keys=[self.key(head)],
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
