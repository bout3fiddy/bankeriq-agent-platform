"""Session reads and acceptance with PostgreSQL authority and disposable Redis views."""

import json
from collections.abc import Callable
from time import monotonic
from typing import Any, Literal

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.context import RunContext
from agent_harness.storage.codec import encode_items, fingerprint, plain
from agent_harness.storage.interface import StateConflictError, StateNotFoundError
from agent_runtime.sessions.cache import RedisSessions
from agent_runtime.storage.history import PostgresHistory, ProjectionPolicy, encoded_size
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.telemetry import emit


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
            emit("cache.miss", context)
            token = self.cache.acquire_refill(head)

            try:

                # Lock contention never blocks correctness on Redis. Concurrent
                # readers may rebuild the same bounded window; publish rejects
                # an older cursor if another reader already filled a newer one.
                view = self.history.view(context)
                cached = self.publish(context, view)
                emit("cache.rebuilt", context)
            finally:
                if token is not None:
                    self.cache.release_refill(head, token)
        else:
            emit("cache.hit", context)

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
            "progress": self.cache.progress(head),
        }

    def record_activity(self, context: RunContext) -> bool:
        """Record an authenticated, explicit interaction using server time.

        The API calls this for an accepted message or POST /activity. A future UI
        can send that signal on an explicit open or deliberate history request.
        Passive reads, SSE and background refreshes must not call it.
        Repeated interaction signals within 15 seconds coalesce;
        a live worker may already have extended the same retention deadline.
        """

        head = self.history.head(context)
        renewed = self.cache.touch(head, minimum_interval=min(15, self.cache.ttl_seconds // 3))

        if renewed:
            emit("session.activity", context)

        return renewed

    def inspect(self, context: RunContext) -> dict[str, Any]:
        """Read owned metadata and Redis TTLs without renewing activity or history.

        A client can compare view_ttl_ms before/after an hour away. Redis -2 means
        the key is absent. Journal/run counts show durable state still exists;
        no transcript, model prompt, tool payload or credential enters this view.
        """

        started = monotonic()

        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            clock_query_started = monotonic()
            database_time = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            clock_query_finished = monotonic()
            assert database_time is not None
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM conversation_events "
                "WHERE tenant_id=%s AND conversation_id=%s) AS journal_events,"
                "(SELECT count(*) FROM runs WHERE tenant_id=%s AND conversation_id=%s) AS runs",
                (*self.store._scope(context), *self.store._scope(context)),
            ).fetchone()
            jobs = connection.execute(
                "SELECT status,count(*) AS count FROM jobs "
                "WHERE tenant_id=%s AND conversation_id=%s GROUP BY status ORDER BY status",
                self.store._scope(context),
            ).fetchall()
            memory = connection.execute(
                "SELECT count(*) AS summary_versions FROM semantic_summaries "
                "WHERE tenant_id=%s AND conversation_id=%s",
                self.store._scope(context),
            ).fetchone()
            usage = connection.execute(
                "SELECT count(*) AS model_calls,COALESCE(sum(input_tokens),0) AS input_tokens,"
                "COALESCE(sum(elapsed_ms),0) AS model_elapsed_ms,"
                "COALESCE(sum(output_tokens),0) AS output_tokens,count(*) FILTER "
                "(WHERE input_tokens IS NULL OR output_tokens IS NULL) AS unknown_usage "
                "FROM model_usage WHERE tenant_id=%s AND conversation_id=%s",
                self.store._scope(context),
            ).fetchone()
            assert usage is not None
            usage = {key: int(value) for key, value in usage.items()}
            expected = connection.execute(
                "SELECT COALESCE(sum((checkpoint->>'model_steps')::int),0) AS steps "
                "FROM runs WHERE tenant_id=%s AND conversation_id=%s "
                "AND status IN ('completed','failed','cancelled','limit_reached')",
                self.store._scope(context),
            ).fetchone()
            assert expected is not None and memory is not None
            usage["expected_calls_lower_bound"] = (
                int(expected["steps"]) + memory["summary_versions"]
            )

        cache = self.cache.inspect(head)

        # The primary timestamp occurs somewhere inside its query round trip.
        # Report only the known work before and after that interval. A remote
        # observer can remove those durations from its clock-offset uncertainty
        # without assuming symmetric network latency or synchronized host clocks.
        # These passive numeric measurements neither renew activity nor read text.
        return {
            "database_time": database_time["now"].isoformat(),
            "database_clock_timing": {
                "before_seconds": clock_query_started - started,
                "after_seconds": monotonic() - clock_query_finished,
            },
            "cursor": head["cursor"],
            "active_run": head["active_run_id"],
            "durable": counts,
            "jobs": jobs,
            "memory": memory,
            "usage": usage,
            "cache": cache,
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
        attempt: int = 0,
        model_tokens: str = "",
        status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"],
    ) -> None:
        if (
            sequence < 1
            or len(model_tokens.encode()) > 8192
            or attempt < 0
            or status
            not in {
                "queued",
                "running",
                "waiting",
                "completed",
                "failed",
                "cancelled",
            }
        ):
            raise ValueError("invalid provisional progress")

        # Verify this run exists and is owned; callers supply fixed status codes,
        # never unrestricted worker text or confidential tool output.
        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            row = connection.execute(
                "SELECT admission_sequence FROM runs WHERE tenant_id = %s "
                "AND conversation_id = %s AND run_id = %s",
                self.store._key(context),
            ).fetchone()

            if row is None:
                raise StateNotFoundError("run unavailable")

        self.cache.progress(
            head,
            sequence=str(sequence),
            progress_generation=str(row["admission_sequence"]),
            attempt=str(attempt),
            progress=json.dumps(
                {
                    "run_id": context.run_id,
                    "sequence": sequence,
                    "attempt": attempt,
                    "model_tokens": model_tokens,
                    "status": status,
                    "provisional": True,
                }
            ),
        )

    def progress_view(self, context: RunContext) -> dict[str, Any]:
        """Read safe run metadata without reconstructing or copying conversation history.

        The conversation lock checks tenant/user/deletion and protects a consistent
        child/status snapshot. For example, call-b can show completed while call-a
        still runs; only the ordered durable join decides the next model step.
        Results and arguments stay outside this view; delivery remains separate.
        """

        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            run = connection.execute(
                "SELECT revision,status FROM runs WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s",
                self.store._key(context),
            ).fetchone()

            if run is None:
                raise StateNotFoundError("run unavailable")

            children = connection.execute(
                "SELECT call_id,status,attempt,ownership_token,failure_reason,effect_pending "
                "FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                "AND group_id IS NOT NULL ORDER BY input_revision,call_position LIMIT 100",
                self.store._key(context),
            ).fetchall()
            deliveries = connection.execute(
                "SELECT delivery FROM results WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s AND delivery IS NOT NULL ORDER BY call_id",
                self.store._key(context),
            ).fetchall()

        # Passive status reads and SSE polling do not signal user interaction.
        # They may remain connected for hours after the Redis view has expired.
        return {
            "run_id": context.run_id,
            **run,
            "disposable": True,
            "children": children,
            "deliveries": [row["delivery"] for row in deliveries],
            "progress": self.cache.progress(head),
        }

    def stream_snapshot(self, context: RunContext) -> dict[str, Any]:
        """Read one owned run's terminal answer or currently fenced provisional text.

        Final output comes from the committed journal even after Redis eviction.
        An old producer may recreate its Redis key after cancellation; comparison
        with the live job token below makes that text ineligible for publication.
        No history reconstruction, transcript projection or activity renewal occurs.
        """

        with self.store._connect() as connection:
            self.store._generation(connection)

            # One statement supplies a committed snapshot of owner, run, current
            # producer and final answer. PostgreSQL MVCC keeps these rows consistent
            # without locking the conversation against admission or settlement.
            # For example, an uncommitted cancellation leaves the previous snapshot
            # visible; the next read after commit sees cancelled and rejects its draft.
            # The shared generation lock still prevents a restore cutover mid-read.
            row = connection.execute(
                "SELECT c.tenant_id,c.conversation_id,c.user_id,c.deletion_generation,"
                "c.deleted,c.cursor,r.status,r.revision,r.terminal_written_at, "
                "CASE WHEN current_setting('track_commit_timestamp')='on' "
                "THEN pg_xact_commit_timestamp(r.terminal_xid) END AS committed_at, "
                "j.job_id,j.ownership_token,j.step,a.answer "
                "FROM conversations c LEFT JOIN runs r ON r.tenant_id=c.tenant_id "
                "AND r.conversation_id=c.conversation_id AND r.run_id=%s "
                "LEFT JOIN LATERAL (SELECT job_id,ownership_token,"
                "recovery->'arguments'->>'step' AS step FROM jobs "
                "WHERE tenant_id=r.tenant_id AND conversation_id=r.conversation_id "
                "AND run_id=r.run_id AND operation='model_infer' AND status='leased' "
                "AND lease_expiry>clock_timestamp() AND input_revision=r.revision LIMIT 1) j "
                "ON r.status NOT IN ('completed','failed','cancelled','limit_reached') "
                "LEFT JOIN LATERAL (SELECT payload->'value'->>'text' AS answer "
                "FROM conversation_events WHERE tenant_id=r.tenant_id "
                "AND conversation_id=r.conversation_id AND run_id=r.run_id "
                "AND payload->>'type'='Message' AND payload->'value'->>'role'='assistant' "
                "ORDER BY sequence DESC LIMIT 1) a ON r.status='completed' "
                "WHERE c.tenant_id=%s AND c.conversation_id=%s",
                (context.run_id, *self.store._scope(context)),
            ).fetchone()

            if row is None or row["user_id"] != context.user_id or row["deleted"]:
                raise StateNotFoundError("conversation unavailable")

            if row["status"] is None:
                raise StateNotFoundError("run unavailable")

            head = {**row, "recovery_id": self.store.recovery_id}
            result = {
                "run_id": context.run_id,
                "status": row["status"],
                "revision": row["revision"],
                "recovery_id": head["recovery_id"],
                "deletion_generation": head["deletion_generation"],
                "cursor": head["cursor"],
                "text": None,
                "final": None,
            }

            if row["status"] in {"completed", "failed", "cancelled", "limit_reached"}:
                result["final"] = {
                    "run_id": context.run_id,
                    "status": row["status"],
                    "answer": row["answer"] if row["status"] == "completed" else None,
                    "cursor": head["cursor"],
                    "committed_at": row["committed_at"].isoformat()
                    if row["committed_at"]
                    else None,
                    "terminal_written_at": row["terminal_written_at"].isoformat()
                    if row["terminal_written_at"]
                    else None,
                    "provisional": False,
                }
                return result

            job = row if row["job_id"] is not None else None

        text = self.cache.text(head, context.run_id) if job is not None else None

        if (
            isinstance(text, dict)
            and job is not None
            and text.get("job_id") == job["job_id"]
            and text.get("attempt") == str(job["ownership_token"])
            and text.get("step") == job["step"]
            and text.get("run_id") == context.run_id
        ):
            result["text"] = text

        return result

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

    def keep_active(
        self, *, limit: int = 100, after: tuple[str, str] = ("", "")
    ) -> tuple[int, tuple[str, str] | None]:
        """Renew a bounded page of conversations with live durable worker leases.

        The application schedules pages more frequently than the cache TTL and
        passes next_scope into the next call, restarting at ("", "") after None.
        For example, keep_active(limit=10) can retain a waiting parent while its
        tool holds a lease, even with no connected browser or new model tokens.
        Queued, approval, expired and cancelled jobs alone do not prove activity.
        """

        if not 1 <= limit <= 100:
            raise ValueError("invalid active session batch bound")

        with self.store._connect() as connection:
            self.store._generation(connection)

            # Scan the existing partial leased-job index, excluding deleted
            # conversations and old runs. No transcript or job payload is loaded.
            # A stale scan can extend retention once; finite TTLs bound that race.
            heads = connection.execute(
                "SELECT DISTINCT c.tenant_id,c.user_id,c.conversation_id,"
                "c.deletion_generation FROM jobs j JOIN conversations c "
                "USING (tenant_id,conversation_id) WHERE j.status='leased' "
                "AND j.recovery IS NOT NULL AND j.lease_expiry>clock_timestamp() "
                "AND NOT c.deleted AND c.active_run_id=j.run_id "
                "AND (c.tenant_id,c.conversation_id)>(%s,%s) "
                "ORDER BY c.tenant_id,c.conversation_id LIMIT %s",
                (*after, limit),
            ).fetchall()

        for head in heads:
            self.cache.touch({**head, "recovery_id": self.store.recovery_id})

        next_scope = (
            (heads[-1]["tenant_id"], heads[-1]["conversation_id"]) if len(heads) == limit else None
        )
        return len(heads), next_scope

    def cleanup_idle(
        self,
        *,
        idle_seconds: int | None = None,
        limit: int = 100,
        after: tuple[str, str] = ("", ""),
    ) -> tuple[int, tuple[str, str] | None]:
        idle_seconds = self.cache.ttl_seconds if idle_seconds is None else idle_seconds
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
        self, context: RunContext, observed: dict[str, Any], *, idle_seconds: int | None = None
    ) -> bool:
        idle_seconds = self.cache.ttl_seconds if idle_seconds is None else idle_seconds

        if idle_seconds < 1:
            raise ValueError("idle threshold must be positive")

        with self.store._connect() as connection:
            head = self.store._head(connection, context, allow_deleted=True)
            active = connection.execute(
                "SELECT 1 FROM jobs WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s AND status='leased' AND lease_expiry>clock_timestamp() LIMIT 1",
                (*self.store._scope(context), head["active_run_id"]),
            ).fetchone()

            # A long tool can leave the conversation journal unchanged while its
            # lease keeps renewing. Do not evict its view between retention sweeps.
            if active is not None:
                return False

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
