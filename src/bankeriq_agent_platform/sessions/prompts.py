"""Optional Redis transport for immutable global prompt bodies."""

from contextlib import suppress
from typing import cast

from redis.exceptions import RedisError

from agent_runtime.prompts.resolution import PromptManifest
from bankeriq_agent_platform.storage.codec import fingerprint

from .cache import RedisSessions


class RedisPromptBodies:
    """Borrow the application-owned Redis client; never cache active bindings.

    Example: PostgresPromptResolver(dsn, scope, body_cache=RedisPromptBodies(cache))
    shares v7 between processes. A Redis outage gives a miss; the resolver reads
    PostgreSQL and checks the saved hash. No conversation data enters these keys.
    """

    def __init__(self, cache: RedisSessions) -> None:
        self.client = cache.client

    @staticmethod
    def key(manifest: PromptManifest) -> str:
        identity = [
            manifest.scope.application,
            manifest.scope.environment,
            manifest.version,
            manifest.content_hash,
        ]
        return "bankeriq:prompt:v1:" + fingerprint(identity)

    def get(self, manifest: PromptManifest) -> str | None:
        try:
            raw = cast(bytes | None, self.client.get(self.key(manifest)))

            if raw is not None and len(raw) <= 32768:
                return raw.decode("utf-8")
        except RedisError, UnicodeError:
            pass

        return None

    def put(self, manifest: PromptManifest, body: str) -> None:
        with suppress(RedisError):
            self.client.set(self.key(manifest), body, ex=1800)
