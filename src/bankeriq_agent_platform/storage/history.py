"""Bounded primary reads and explicit asynchronous history materialisation."""

import json
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from agent_runtime.conversations.context import ContextBudgetExceeded
from agent_runtime.runs.context import RunContext
from agent_runtime.storage.interface import StateConflictError

from .postgres import Database, PostgresStateStore

MAX_ITEMS = 200
MAX_BYTES = 262144


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def recent_records(records: list[dict[str, Any]], *, incomplete_first: bool = False) -> list[dict]:
    """Keep complete recent turns, including every call/result in the newest turn.

    Input is ordered by admission sequence then event sequence. If a bounded SQL
    read clipped the oldest turn, discard that turn altogether. With turns A/B/C,
    dropping A retains B/C; dropping half of B could orphan B's tool results.
    A single newest turn above the bound requires explicit compaction and fails.
    """

    groups: list[list[dict]] = []

    for record in records:
        if not groups or groups[-1][0]["run_id"] != record["run_id"]:
            groups.append([])

        groups[-1].append(record)

    if incomplete_first and groups:
        if len(groups) == 1:
            raise ContextBudgetExceeded("current turn exceeds context bound; compaction required")

        groups.pop(0)

    selected: list[dict] = []

    for group in reversed(groups):
        candidate = group + selected

        if len(candidate) > MAX_ITEMS or encoded_size(candidate) > MAX_BYTES:
            if not selected:
                raise ContextBudgetExceeded(
                    "current turn exceeds context bound; compaction required"
                )

            break

        selected = candidate

    return selected


@dataclass(frozen=True)
class ProjectionPolicy:
    seconds: int = 60
    events: int = 100
    bytes: int = 1048576
    idle_seconds: int = 900

    def __post_init__(self) -> None:
        if min(self.seconds, self.events, self.bytes, self.idle_seconds) < 1:
            raise ValueError("projection thresholds must be positive")


class PostgresHistory:
    """Share the journal adapter's primary routing, generation fence and lock ordering."""

    def __init__(self, store: PostgresStateStore) -> None:
        self.store = store

    def head(self, context: RunContext, *, allow_deleted: bool = False) -> dict[str, Any]:
        with self.store._connect() as connection:
            return self.store._head(connection, context, allow_deleted=allow_deleted)

    def view(self, context: RunContext) -> dict[str, Any]:
        with self.store._connect() as connection:
            head = self.store._head(connection, context)
            return self._view(connection, context, head)

    def _view(
        self, connection: Database, context: RunContext, head: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = connection.execute(
            "SELECT * FROM conversation_snapshots WHERE tenant_id = %s AND conversation_id = %s",
            self.store._scope(context),
        ).fetchone()
        records: list[dict] | None = None
        complete = True

        if (
            snapshot is not None
            and snapshot["recovery_id"] == self.store.recovery_id
            and snapshot["deletion_generation"] == head["deletion_generation"]
            and snapshot["cursor"] <= head["cursor"]
            and snapshot["payload"].get("context_complete", True)
        ):
            if snapshot["format_version"] != 1:
                raise ValueError("unsupported history snapshot schema")

            tail = connection.execute(
                "SELECT e.sequence, e.run_id, r.admission_sequence, e.payload "
                "FROM conversation_events e JOIN runs r USING "
                "(tenant_id, conversation_id, run_id) WHERE e.tenant_id = %s "
                "AND e.conversation_id = %s AND e.sequence > %s AND e.sequence <= %s "
                "ORDER BY e.sequence LIMIT 201",
                (*self.store._scope(context), snapshot["cursor"], head["cursor"]),
            ).fetchall()

            # A busy session can outrun its snapshot. Bound the tail read, then
            # fall back to an indexed recent-window read when 201 rows arrive.
            # Normal hydration reuses the committed snapshot plus <=200 deltas.
            known_runs = {row["run_id"] for row in snapshot["payload"]["events"]}
            highest_turn = max(
                (row["admission_sequence"] for row in snapshot["payload"]["events"]), default=0
            )
            extends_omitted_turn = any(
                row["run_id"] not in known_runs and row["admission_sequence"] <= highest_turn
                for row in tail
            )

            if len(tail) <= MAX_ITEMS and not extends_omitted_turn:
                try:
                    records = recent_records(
                        sorted(
                            snapshot["payload"]["events"] + tail,
                            key=lambda row: (row["admission_sequence"], row["sequence"]),
                        )
                    )
                except ContextBudgetExceeded:
                    records, complete = [], False

        if records is None:
            try:
                records = self.store._recent_events(connection, context, head, run=False)
            except ContextBudgetExceeded:
                records, complete = [], False

        return {
            "schema_version": 1,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "conversation_id": context.conversation_id,
            "recovery_id": self.store.recovery_id,
            "deletion_generation": head["deletion_generation"],
            "cursor": head["cursor"],
            "revision": head["revision"],
            "active_run_id": head["active_run_id"],
            "events": records,
            "context_complete": complete,
        }

    def page(self, context: RunContext, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid history page bounds")

        with self.store._connect() as connection:
            head = self.store._head(connection, context)

            if after > head["cursor"]:
                raise StateConflictError("history cursor is ahead of the primary")

            page = connection.execute(
                "SELECT * FROM history_pages WHERE tenant_id = %s AND conversation_id = %s "
                "AND first_cursor <= %s AND last_cursor > %s ORDER BY first_cursor LIMIT 1",
                (*self.store._scope(context), after + 1, after),
            ).fetchone()

            if page is not None:
                if page["format_version"] != 1:
                    raise ValueError("unsupported history page schema")

                records = [row for row in page["payload"]["events"] if row["sequence"] > after]
                end = page["last_cursor"]
            else:
                records = connection.execute(
                    "SELECT e.sequence, e.run_id, r.admission_sequence, e.payload "
                    "FROM conversation_events e JOIN runs r USING "
                    "(tenant_id, conversation_id, run_id) WHERE e.tenant_id = %s "
                    "AND e.conversation_id = %s AND e.sequence > %s "
                    "ORDER BY e.sequence LIMIT %s",
                    (*self.store._scope(context), after, limit),
                ).fetchall()
                end = records[-1]["sequence"] if len(records) == limit else head["cursor"]

            selected: list[dict] = []

            for record in records[:limit]:
                if encoded_size(selected + [record]) > 800000:
                    break

                selected.append(record)

            if len(selected) < len(records):
                end = selected[-1]["sequence"]

            return {
                "schema_version": 1,
                "events": selected,
                "next_cursor": end,
                "durable_cursor": head["cursor"],
                "recovery_id": self.store.recovery_id,
                "deletion_generation": head["deletion_generation"],
            }

    def materialize(self, context: RunContext) -> dict[str, Any] | None:
        """Commit one <=100-cursor page and recent snapshot before returning to Redis.

        Example: dirty=250, projected=0 produces page 1..100. A failure after
        page insertion rolls back the snapshot and progress too. A retry starts
        from the last committed cursor. Writers arriving after this transaction
        commit increase dirty_cursor without losing the earlier progress.
        """

        target_cursor: int | None = None

        def transaction(connection: Database) -> dict[str, Any] | None:
            nonlocal target_cursor
            head = self.store._head(connection, context, allow_deleted=True)
            work = connection.execute(
                "SELECT * FROM projection_work WHERE tenant_id = %s AND conversation_id = %s "
                "FOR UPDATE",
                self.store._scope(context),
            ).fetchone()

            if head["deleted"]:
                return None

            if (
                work is None
                or work["projected_cursor"] >= head["cursor"]
                or (target_cursor is not None and work["projected_cursor"] >= target_cursor)
            ):
                return self._view(connection, context, head)

            first = work["projected_cursor"] + 1
            end = min(first + 99, head["cursor"], target_cursor or head["cursor"])
            records = connection.execute(
                "SELECT e.sequence, e.run_id, r.admission_sequence, e.payload "
                "FROM conversation_events e JOIN runs r USING "
                "(tenant_id, conversation_id, run_id) WHERE e.tenant_id = %s "
                "AND e.conversation_id = %s AND e.sequence BETWEEN %s AND %s ORDER BY e.sequence",
                (*self.store._scope(context), first, end),
            ).fetchall()
            selected: list[dict] = []

            for record in records:
                if encoded_size(selected + [record]) > 800000:
                    end = selected[-1]["sequence"]
                    break

                selected.append(record)

            target_cursor = end
            self.store._write(
                connection,
                "INSERT INTO history_pages VALUES (%s, %s, %s, %s, 1, %s)",
                (
                    *self.store._scope(context),
                    first,
                    end,
                    Jsonb({"schema_version": 1, "events": selected}),
                ),
            )
            fixed_head = {**head, "cursor": end}
            view = self._view(connection, context, fixed_head)
            self.store._write(
                connection,
                "INSERT INTO conversation_snapshots VALUES (%s, %s, %s, %s, %s, 1, %s) "
                "ON CONFLICT (tenant_id, conversation_id) DO UPDATE SET "
                "cursor = EXCLUDED.cursor, deletion_generation = EXCLUDED.deletion_generation, "
                "recovery_id = EXCLUDED.recovery_id, payload = EXCLUDED.payload",
                (
                    *self.store._scope(context),
                    end,
                    head["deletion_generation"],
                    self.store.recovery_id,
                    Jsonb(view),
                ),
            )
            self.store._write(
                connection,
                "UPDATE projection_work SET projected_cursor = %s, materialized_at = now(), "
                "pending_events = GREATEST(0, pending_events - %s), "
                "pending_bytes = GREATEST(0, pending_bytes - %s) "
                "WHERE tenant_id = %s AND conversation_id = %s",
                (
                    end,
                    len(selected),
                    sum(encoded_size(row["payload"]) for row in selected),
                    *self.store._scope(context),
                ),
            )
            return view

        return self.store._retry(transaction)

    def acknowledge_cache(self, context: RunContext, cursor: int, deletion_generation: int) -> None:
        with self.store._connect() as connection:
            head = self.store._head(connection, context, allow_deleted=True)

            if head["deletion_generation"] != deletion_generation or cursor > head["cursor"]:
                raise StateConflictError("cache acknowledgement generation or cursor changed")

            connection.execute(
                "UPDATE projection_work SET cache_cursor = GREATEST(cache_cursor, %s), "
                "recovery_id = %s, projected_cursor = CASE WHEN %s THEN %s "
                "ELSE projected_cursor END "
                "WHERE tenant_id = %s AND conversation_id = %s",
                (
                    cursor,
                    self.store.recovery_id,
                    head["deleted"],
                    head["cursor"],
                    *self.store._scope(context),
                ),
            )

    def due(self, policy: ProjectionPolicy, *, limit: int = 100) -> list[RunContext]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid projection batch bound")

        with self.store._connect() as connection:
            self.store._generation(connection)
            rows = connection.execute(
                "SELECT c.tenant_id, c.user_id, c.conversation_id FROM projection_work p "
                "JOIN conversations c USING (tenant_id, conversation_id) WHERE "
                "(p.dirty_cursor > p.projected_cursor OR p.dirty_cursor > p.cache_cursor "
                "OR p.recovery_id <> %s) AND (c.deleted OR p.recovery_id <> %s "
                "OR p.pending_events >= %s OR p.pending_bytes >= %s "
                "OR p.dirty_since <= now() - %s * interval '1 second' "
                "OR c.last_activity <= now() - %s * interval '1 second') "
                "ORDER BY (p.dirty_cursor > p.projected_cursor) DESC, "
                "COALESCE(p.materialized_at, p.dirty_since), "
                "p.tenant_id, p.conversation_id LIMIT %s",
                (
                    self.store.recovery_id,
                    self.store.recovery_id,
                    policy.events,
                    policy.bytes,
                    policy.seconds,
                    policy.idle_seconds,
                    limit,
                ),
            ).fetchall()
            return [RunContext(**row, run_id="") for row in rows]

    def idle(
        self, *, seconds: int = 900, limit: int = 100, after: tuple[str, str] = ("", "")
    ) -> list[RunContext]:
        if seconds < 1 or not 1 <= limit <= 100:
            raise ValueError("invalid idle scan bounds")

        with self.store._connect() as connection:
            self.store._generation(connection)
            rows = connection.execute(
                "SELECT tenant_id, user_id, conversation_id FROM conversations "
                "WHERE last_activity <= now() - %s * interval '1 second' "
                "AND (tenant_id, conversation_id) > (%s, %s) "
                "ORDER BY tenant_id, conversation_id LIMIT %s",
                (seconds, *after, limit),
            ).fetchall()
            return [RunContext(**row, run_id="") for row in rows]
