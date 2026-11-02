"""Session reads and acceptance with PostgreSQL authority and disposable Redis views."""

import json
from collections.abc import Callable
from typing import Any, Literal

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.runs.context import RunContext
from agent_runtime.storage.interface import StateConflictError, StateNotFoundError
from bankeriq_agent_platform.storage.codec import encode_items, fingerprint, plain
from bankeriq_agent_platform.storage.history import PostgresHistory, ProjectionPolicy, encoded_size
from bankeriq_agent_platform.storage.postgres import PostgresStateStore

from .cache import RedisSessions


class SessionService:
    """Trusted composition supplies identity/input policy; Redis cannot execute a run.

    stage() makes permitted text provisional. admit() returns acceptance only after
    the journal, request fingerprint and queued job commit. A failed refresh never
    rolls that commit back. Explicit project_due() drives asynchronous work in tests;
    this class starts no thread, timer, worker lease or external tool operation.
    """

    def __init__(
        self,
        store: PostgresStateStore,
        cache: RedisSessions,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.history = PostgresHistory(store)
        self.cache = cache
        self._fault = fault or (lambda phase: None)

    def read(self, context: RunContext, *, minimum_cursor: int = 0) -> dict[str, Any]:
        head = self.history.head(context)

        if minimum_cursor < 0 or minimum_cursor > head["cursor"]:
            raise StateConflictError("accepted cursor is ahead of the primary")

        cached = self.cache.apply("read", head)
        view = self._cached_view(cached, head)

        if view is None:
            token = self.cache.acquire_refill(head)

            try:

                # Lock contention never blocks correctness on Redis. Concurrent
                # readers may rebuild the same bounded window; publish rejects
                # an older cursor if another reader already filled a newer one.
                view = self.history.view(context)
                cached = self.publish(context, view, touch=True)
            finally:
                if token is not None:
                    self.cache.release_refill(head, token)

        pending = list((cached or {}).get("pending", {}).values())
        reconciled = []
        visible_pending = []

        for raw in pending:
            entry = json.loads(raw)

            try:
                accepted = self._admission(context, entry["request_id"], entry["fingerprint"])
            except StateConflictError:

                # A provisional display has no durable reservation. If another
                # request won this ID after cache loss, report that conflict once
                # and remove the obsolete display without hiding accepted history.
                visible_pending.append({**entry, "status": "conflict"})
                reconciled.append(fingerprint(entry["request_id"]))
                continue

            if accepted is not None:
                reconciled.append(fingerprint(entry["request_id"]))
            else:
                visible_pending.append(entry)

        if reconciled:
            self.cache.apply("reconcile", head, keys=reconciled)

        return {
            **view,
            "pending": visible_pending,
            "progress": [json.loads(raw) for raw in (cached or {}).get("progress", [])],
        }

    def publish(
        self, context: RunContext, view: dict[str, Any], *, touch: bool = False
    ) -> dict[str, Any] | None:
        head = self.history.head(context, allow_deleted=True)

        if head["deleted"]:
            return self.cache.apply("tombstone", head)

        if (
            view["recovery_id"] != head["recovery_id"]
            or view["deletion_generation"] != head["deletion_generation"]
            or any(view[key] != head[key] for key in ("tenant_id", "conversation_id", "user_id"))
            or view["cursor"] > head["cursor"]
        ):
            raise StateConflictError("session scope, generation or cursor changed")

        return self.cache.apply(
            "publish", head, cursor=str(view["cursor"]), view=json.dumps(view), touch=touch
        )

    def stage(self, context: RunContext, *, request_id: str, text: str) -> dict[str, Any]:
        head = self.history.head(context)
        digest = self._fingerprint(context, request_id, text)
        accepted = self._admission(context, request_id, digest)

        if accepted is not None:
            return accepted

        entry = {
            "request_id": request_id,
            "run_id": context.run_id,
            "fingerprint": digest,
            "text": text,
            "status": "provisional",
        }
        result = self.cache.apply(
            "stage",
            head,
            request_key=fingerprint(request_id),
            fingerprint=digest,
            pending=json.dumps(entry),
        )
        return {"request_id": request_id, "accepted": False, "display_cached": result is not None}

    def admit(self, context: RunContext, *, request_id: str, text: str) -> dict[str, Any]:
        digest = self._fingerprint(context, request_id, text)

        # Redis-only staged entries are advisory. PostgreSQL decides stable-ID
        # matching and conflict even if Redis vanished between stage and admit.
        self.store.admit(context, request_id=request_id, items=(Message(MessageRole.USER, text),))
        accepted = self._admission(context, request_id, digest)
        assert accepted is not None
        self._fault("after_admission")

        try:
            self.publish(context, self.history.view(context), touch=True)
            self.cache.apply(
                "reconcile", self.history.head(context), keys=[fingerprint(request_id)]
            )
        except Exception:

            # No refresh failure can turn durable acceptance into a rejection.
            # The committed dirty cursor remains available for explicit repair.
            pass

        return accepted

    def history_page(
        self, context: RunContext, *, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        return self.history.page(context, after=after, limit=limit)

    def progress(
        self,
        context: RunContext,
        *,
        sequence: int,
        status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"],
    ) -> None:
        if sequence < 1 or status not in {
            "queued",
            "running",
            "waiting",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("invalid provisional progress")

        # Verify this run exists and is owned; callers supply fixed status codes,
        # never unrestricted worker text or confidential tool output.
        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            row = connection.execute(
                "SELECT 1 FROM runs WHERE tenant_id = %s AND conversation_id = %s AND run_id = %s",
                self.store._key(context),
            ).fetchone()

            if row is None:
                raise StateNotFoundError("run unavailable")

        self.cache.apply(
            "progress",
            head,
            sequence=str(sequence),
            progress=json.dumps(
                {
                    "run_id": context.run_id,
                    "sequence": sequence,
                    "status": status,
                    "provisional": True,
                }
            ),
        )

    def project_due(self, policy: ProjectionPolicy | None = None, *, limit: int = 100) -> int:
        contexts = self.history.due(policy or ProjectionPolicy(), limit=limit)

        for context in contexts:
            self.history.materialize(context)
            head = self.history.head(context, allow_deleted=True)
            result = (
                self.cache.apply("tombstone", head)
                if head["deleted"]
                else self.publish(context, self.history.view(context))
            )
            self._fault("after_cache_update")

            if result is not None:
                self.history.acknowledge_cache(
                    context, int(result["cursor"]), int(result["generation"])
                )

        return len(contexts)

    def cleanup_idle(
        self,
        *,
        idle_seconds: int = 900,
        limit: int = 100,
        after: tuple[str, str] = ("", ""),
    ) -> tuple[int, tuple[str, str] | None]:
        removed = 0
        contexts = self.history.idle(seconds=idle_seconds, limit=limit, after=after)

        # Advance the keyset cursor even when Redis is absent. Otherwise the same
        # first 100 expired sessions could starve cleanup of all later sessions.
        # A fresh sweep starts with after=("", ""); callers schedule sweeps externally.
        for context in contexts:
            head = self.history.head(context, allow_deleted=True)
            observed = self.cache.apply("peek", head)

            if observed is not None and self.cleanup(context, observed, idle_seconds=idle_seconds):
                removed += 1

        next_scope = (
            (contexts[-1].tenant_id, contexts[-1].conversation_id)
            if len(contexts) == limit
            else None
        )
        return removed, next_scope

    def cleanup(
        self, context: RunContext, observed: dict[str, Any], *, idle_seconds: int = 900
    ) -> bool:
        if idle_seconds < 1:
            raise ValueError("idle threshold must be positive")

        with self.store._connect() as connection:
            head = self.store._head(connection, context, allow_deleted=True)
            work = connection.execute(
                "SELECT projected_cursor FROM projection_work "
                "WHERE tenant_id = %s AND conversation_id = %s",
                self.store._scope(context),
            ).fetchone()

            # Explicit idle eviction waits for the final durable snapshot.
            # Example: head=250/projected=200 stays cached until the next batch.
            # Redis's independent TTL may still expire; the journal is retained.
            if (
                not head["deleted"]
                and work is not None
                and work["projected_cursor"] < head["cursor"]
            ):
                return False

        if (
            head["idle_seconds"] < idle_seconds
            or str(head["cursor"]) != observed["cursor"]
            or str(head["deletion_generation"]) != observed["generation"]
        ):
            return False

        result = self.cache.apply(
            "cleanup", head, epoch=observed["epoch"], idle_seconds=idle_seconds
        )
        return result is not None

    def _admission(
        self, context: RunContext, request_id: str, digest: str
    ) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            row = connection.execute(
                "SELECT a.*, r.status FROM admissions a JOIN runs r USING "
                "(tenant_id, conversation_id, run_id) WHERE a.tenant_id = %s AND a.request_id = %s",
                (context.tenant_id, request_id),
            ).fetchone()

            if row is None:
                return None

            if row["fingerprint"] != digest or row["conversation_id"] != context.conversation_id:
                raise StateConflictError("request ID reused with different input")

            return {
                "request_id": request_id,
                "accepted": True,
                "run_id": row["run_id"],
                "admission_sequence": row["admission_sequence"],
                "cursor": row["accepted_cursor"],
                "status": row["status"],
                "recovery_id": head["recovery_id"],
                "deletion_generation": head["deletion_generation"],
            }

    @staticmethod
    def _fingerprint(context: RunContext, request_id: str, text: str) -> str:
        if not request_id or len(request_id) > 256 or not text.strip():
            raise ValueError("bounded request ID and permitted text are required")

        encoded = encode_items((Message(MessageRole.USER, text),))

        if encoded_size(encoded) > 32768:
            raise ValueError("provisional input exceeds 32 KiB")

        return fingerprint([plain(context), encoded])

    @staticmethod
    def _cached_view(cached: dict[str, Any] | None, head: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if cached is None or cached["schema"] != "1" or cached["deleted"]:
                return None

            view = json.loads(cached["view"])

            if view["schema_version"] != 1 or any(
                view[key] != head[key]
                for key in (
                    "tenant_id",
                    "user_id",
                    "conversation_id",
                    "recovery_id",
                    "deletion_generation",
                    "cursor",
                )
            ):
                return None

            return view
        except KeyError, TypeError, ValueError:
            return None
