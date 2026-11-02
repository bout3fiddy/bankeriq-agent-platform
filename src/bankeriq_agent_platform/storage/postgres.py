"""Primary-only PostgreSQL journal adapter for the synchronous synthetic runtime."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, LiteralString
from uuid import uuid4

import psycopg
from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure, UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_runtime.conversations.conversation import Conversation, ConversationItem
from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.prompts.resolution import PromptManifest, PromptResolver, PromptScope
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import TERMINAL_STATUSES, RunState, RunStatus, validate_transition
from agent_runtime.storage.interface import (
    Checkpoint,
    HistoryPage,
    StateConflictError,
    StateNotFoundError,
)
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    ToolFailure,
    VisibleResult,
    WaitReason,
)

from .codec import decode_item, encode_items, fingerprint, plain

type Database = Connection[dict[str, Any]]


class CommitUnknownError(RuntimeError):
    """Retry the same stable identity on the primary; success was not established."""


def migrate(dsn: str) -> None:
    """Apply immutable numbered migrations under one transaction and migration lock."""

    with Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(720321)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions "
            "(version text PRIMARY KEY, fingerprint text NOT NULL)"
        )

        for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            source = path.read_text()
            digest = fingerprint(source)
            existing = connection.execute(
                "SELECT fingerprint FROM schema_versions WHERE version = %s", (path.name,)
            ).fetchone()

            if existing:
                if existing["fingerprint"] != digest:
                    raise StateConflictError("applied migration changed")

                continue

            connection.execute(source.encode())
            connection.execute("INSERT INTO schema_versions VALUES (%s, %s)", (path.name, digest))

        connection.execute(
            "INSERT INTO runtime_generation VALUES (true, %s) ON CONFLICT DO NOTHING",
            (str(uuid4()),),
        )


class PostgresStateStore:
    """Inject into open_runtime(store=...). Each operation uses a short primary connection.

    fault is a test seam, e.g. fault=lambda phase: raise_after_third_write(phase).
    No model, tool, permission resolution or scheduling runs inside these transactions.
    """

    def __init__(self, dsn: str, *, fault: Callable[[str], None] | None = None) -> None:
        self._dsn = dsn
        self._fault = fault or (lambda phase: None)

        with self._connect() as connection:
            row = connection.execute("SELECT recovery_id FROM runtime_generation").fetchone()
            assert row is not None
            self.recovery_id: str = row["recovery_id"]

    def create_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        with self._connect() as connection:
            self._generation(connection)

            try:
                self._write(
                    connection,
                    "INSERT INTO conversations (tenant_id, conversation_id, user_id) "
                    "VALUES (%s, %s, %s)",
                    (tenant_id, conversation_id, user_id),
                )
            except UniqueViolation:
                raise StateConflictError("conversation already exists") from None

        return Conversation(tenant_id, user_id, conversation_id)

    def load_conversation(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> Conversation:
        context = RunContext(tenant_id, user_id, conversation_id, "")

        with self._connect() as connection:
            head = self._head(connection, context)
            return self._conversation(connection, context, head)

    def admit(
        self, context: RunContext, *, request_id: str, items: tuple[ConversationItem, ...]
    ) -> Checkpoint:
        """Persist policy-approved user input and ordered work under a stable request ID."""

        if not items or any(
            not isinstance(item, Message) or item.role != MessageRole.USER for item in items
        ):
            raise ValueError("admission requires permitted user messages")

        encoded = encode_items(items)
        digest = fingerprint([plain(context), encoded])

        def transaction(connection: Database) -> Checkpoint:
            head = self._head(connection, context)
            prior = connection.execute(
                "SELECT * FROM admissions WHERE tenant_id = %s AND request_id = %s",
                (context.tenant_id, request_id),
            ).fetchone()

            if prior:
                if prior["fingerprint"] != digest:
                    raise StateConflictError("request ID reused with different input")

                return self._load(connection, context, head)

            sequence = head["admission_sequence"] + 1
            active = head["active_run_id"] is None
            run = RunState(context, RunStatus.QUEUED)
            self._write(
                connection,
                "INSERT INTO runs (tenant_id, conversation_id, run_id, admission_sequence, "
                "active, status, checkpoint) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (*self._key(context), sequence, active, run.status.value, Jsonb(plain(run))),
            )
            self._write(
                connection,
                "INSERT INTO admissions VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    context.tenant_id,
                    request_id,
                    context.conversation_id,
                    context.run_id,
                    digest,
                    sequence,
                ),
            )
            self._append(connection, context, head, encoded, f"admit:{request_id}")
            self._write(
                connection,
                "UPDATE conversations SET admission_sequence = %s, active_run_id = %s "
                "WHERE tenant_id = %s AND conversation_id = %s",
                (
                    sequence,
                    context.run_id if active else head["active_run_id"],
                    *self._scope(context),
                ),
            )
            head["active_run_id"] = context.run_id if active else head["active_run_id"]
            self._job(connection, context, 0, "ready" if active else "blocked")
            self._dirty(connection, context, head)
            return self._load(connection, context, head)

        return self._retry(transaction)

    def create_run(
        self,
        context: RunContext,
        *,
        expected_conversation_revision: int,
        items: tuple[ConversationItem, ...] = (),
    ) -> Checkpoint:
        """Use run_id as request ID; durable ordering supersedes the UI revision."""

        return self.admit(context, request_id=context.run_id, items=items)

    def load_run(self, context: RunContext) -> Checkpoint:
        with self._connect() as connection:
            return self._load(connection, context, self._head(connection, context))

    def save_checkpoint(
        self,
        context: RunContext,
        *,
        expected_conversation_revision: int,
        expected_run_revision: int,
        status: RunStatus,
        items: tuple[ConversationItem, ...] = (),
        pending_calls: tuple[PendingToolCall, ...] = (),
        model_steps: int | None = None,
        tool_calls: int | None = None,
        artifacts: tuple[ArtifactReference, ...] = (),
        expected_recovery_id: str | None = None,
        expected_deletion_generation: int = 0,
        prompt_resolver: PromptResolver | None = None,
    ) -> Checkpoint:
        """Commit bounded delta, budgets, results/delivery and next synthetic work together.

        A run revision identifies one logical transition. Matching replay returns its
        original bounded checkpoint response, even if execution has advanced further.
        """

        if expected_recovery_id is not None and expected_recovery_id != self.recovery_id:
            raise StateConflictError("checkpoint recovery generation changed")

        encoded = encode_items(items)
        encode_items(pending_calls)
        operation_id = f"checkpoint:{context.run_id}:{expected_run_revision}"
        digest = fingerprint(
            [
                plain(context),
                expected_conversation_revision,
                expected_run_revision,
                status,
                encoded,
                plain(pending_calls),
                model_steps,
                tool_calls,
                plain(artifacts),
            ]
            + (["prepare-prompt-v1"] if prompt_resolver is not None else [])
        )

        def transaction(connection: Database) -> Checkpoint:
            head = self._head(connection, context)
            prior = connection.execute(
                "SELECT * FROM transitions WHERE tenant_id = %s AND operation_id = %s",
                (context.tenant_id, operation_id),
            ).fetchone()

            if prior:
                if prior["fingerprint"] != digest:
                    raise StateConflictError("transition ID reused with different checkpoint")

                replay = self._response(connection, context, prior["response"])

                if prompt_resolver is not None:
                    assert replay.run.prompt_manifest is not None
                    prompt_resolver.instructions(replay.run.prompt_manifest)

                return replay

            if head["deletion_generation"] != expected_deletion_generation:
                raise StateConflictError("checkpoint deletion generation changed")

            current = self._load(connection, context, head)

            if (
                head["revision"] != expected_conversation_revision
                or current.run.revision != expected_run_revision
                or head["active_run_id"] != context.run_id
            ):
                raise StateConflictError("checkpoint changed or run is inactive")

            if prompt_resolver is not None and current.run.pending_calls:
                raise StateConflictError("model preparation requires resolved tool calls")

            if prompt_resolver is not None and (
                status is not RunStatus.RUNNING
                or items
                or pending_calls
                or artifacts
                or model_steps != current.run.model_steps + 1
                or tool_calls is not None
            ):
                raise ValueError("prompt preparation must reserve exactly one model step")

            validate_transition(current.run.status, status)
            run = replace(
                current.run,
                status=status,
                pending_calls=pending_calls,
                revision=current.run.revision + 1,
                model_steps=current.run.model_steps if model_steps is None else model_steps,
                tool_calls=current.run.tool_calls if tool_calls is None else tool_calls,
                prompt_manifest=prompt_resolver.resolve()
                if prompt_resolver is not None
                else current.run.prompt_manifest,
            )

            if run.model_steps < current.run.model_steps or run.tool_calls < current.run.tool_calls:
                raise ValueError("budget usage cannot decrease")

            terminal = status in TERMINAL_STATUSES
            self._write(
                connection,
                "UPDATE runs SET revision = %s, status = %s, checkpoint = %s, active = %s "
                "WHERE tenant_id = %s AND conversation_id = %s AND run_id = %s AND revision = %s",
                (
                    run.revision,
                    status.value,
                    Jsonb(plain(run)),
                    not terminal,
                    *self._key(context),
                    expected_run_revision,
                ),
            )
            self._append(connection, context, head, encoded, operation_id)
            self._outcomes(connection, context, items, artifacts)
            self._write(
                connection,
                "UPDATE jobs SET status = 'completed' WHERE tenant_id = %s "
                "AND conversation_id = %s AND run_id = %s AND operation = 'coordinator' "
                "AND status IN ('ready', 'waiting')",
                self._key(context),
            )
            self._pending(connection, context, pending_calls, run.revision)
            head["revision"] += 1

            if terminal:
                head["active_run_id"] = self._activate_next(connection, context)
            else:
                self._job(
                    connection,
                    context,
                    run.revision,
                    "waiting" if status == RunStatus.WAITING else "ready",
                )

            self._write(
                connection,
                "UPDATE conversations SET revision = %s, active_run_id = %s "
                "WHERE tenant_id = %s AND conversation_id = %s AND revision = %s",
                (
                    head["revision"],
                    head["active_run_id"],
                    *self._scope(context),
                    expected_conversation_revision,
                ),
            )
            self._dirty(connection, context, head)
            response = {"head": head, "run": plain(run)}
            self._write(
                connection,
                "INSERT INTO transitions VALUES (%s, %s, %s, %s, %s, %s)",
                (*self._key(context), operation_id, digest, Jsonb(response)),
            )

            if prompt_resolver is not None:
                self._write(
                    connection,
                    "INSERT INTO model_step_prompts VALUES (%s, %s, %s, %s, %s)",
                    (*self._key(context), run.model_steps, operation_id),
                )

            return self._response(connection, context, response)

        return self._retry(transaction)

    def load_model_step(self, context: RunContext, step_number: int) -> Checkpoint:
        with self._connect() as connection:
            self._head(connection, context)
            row = connection.execute(
                "SELECT t.response FROM model_step_prompts s JOIN transitions t "
                "ON (s.tenant_id, s.operation_id) = (t.tenant_id, t.operation_id) "
                "WHERE s.tenant_id = %s AND s.conversation_id = %s AND s.run_id = %s "
                "AND s.step_number = %s",
                (*self._key(context), step_number),
            ).fetchone()

            if row is None:
                raise StateNotFoundError("prepared model step unavailable")

            return self._response(connection, context, row["response"])

    def record_approval(
        self,
        context: RunContext,
        *,
        reference_id: str,
        call: ToolCall,
        operation_version: str,
        granted: bool,
        expires_at: str,
    ) -> None:
        """Persist a trusted application's exact scoped decision and expiry.

        This stores synthetic decisions. The caller owns permission checks;
        worker execution and approval consumption remain later work.
        """

        digest = fingerprint(encode_items((call,))[0])

        def transaction(connection: Database) -> None:
            head = self._head(connection, context)
            checkpoint = self._load(connection, context, head)

            if checkpoint.run.status in TERMINAL_STATUSES:
                raise StateConflictError("approval run is terminal")

            row = connection.execute(
                "SELECT * FROM approvals WHERE tenant_id = %s AND conversation_id = %s "
                "AND run_id = %s AND reference_id = %s FOR UPDATE",
                (*self._key(context), reference_id),
            ).fetchone()

            if row is None or row["argument_fingerprint"] != digest:
                raise StateConflictError("approval does not match original intent")

            if row["decision"] is not None:
                matches = connection.execute(
                    "SELECT decision = %s AND operation_version = %s "
                    "AND expires_at = %s::timestamptz AS matches FROM approvals "
                    "WHERE tenant_id = %s AND conversation_id = %s AND run_id = %s "
                    "AND reference_id = %s",
                    (granted, operation_version, expires_at, *self._key(context), reference_id),
                ).fetchone()

                if matches is None or not matches["matches"]:
                    raise StateConflictError("approval decision conflicts")

                return

            self._write(
                connection,
                "UPDATE approvals SET decision = %s, operation_version = %s, "
                "expires_at = %s::timestamptz WHERE tenant_id = %s AND conversation_id = %s "
                "AND run_id = %s AND reference_id = %s",
                (granted, operation_version, expires_at, *self._key(context), reference_id),
            )
            head["cursor"] += 1
            self._write(
                connection,
                "UPDATE conversations SET cursor = %s WHERE tenant_id = %s "
                "AND conversation_id = %s",
                (head["cursor"], *self._scope(context)),
            )
            self._dirty(connection, context, head)

        self._retry(transaction)

    def delete_conversation(self, context: RunContext) -> int:
        """Fence late writes with a permanent tombstone; retain records for policy review."""

        def transaction(connection: Database) -> int:
            head = self._head(connection, context, allow_deleted=True)

            if head["deleted"]:
                return head["deletion_generation"]

            self._write(
                connection,
                "UPDATE runs SET active = false, status = 'cancelled', revision = revision + 1, "
                "checkpoint = jsonb_set(jsonb_set(jsonb_set(checkpoint, '{status}', "
                "'\"cancelled\"'), '{pending_calls}', '[]'), '{revision}', "
                "to_jsonb(revision + 1)) WHERE tenant_id = %s AND conversation_id = %s "
                "AND status IN ('queued', 'running', 'waiting')",
                self._scope(context),
            )
            self._write(
                connection,
                "UPDATE jobs SET status = 'cancelled' WHERE tenant_id = %s "
                "AND conversation_id = %s AND status IN ('blocked', 'ready', 'waiting')",
                self._scope(context),
            )
            head["deletion_generation"] += 1
            head["cursor"] += 1
            self._write(
                connection,
                "UPDATE conversations SET deleted = true, deletion_generation = %s, "
                "cursor = %s, revision = revision + 1, active_run_id = NULL "
                "WHERE tenant_id = %s AND conversation_id = %s",
                (head["deletion_generation"], head["cursor"], *self._scope(context)),
            )
            self._dirty(connection, context, head)
            return head["deletion_generation"]

        return self._retry(transaction)

    def deliveries(self, context: RunContext) -> tuple[ArtifactReference, ...]:
        """Load opaque delivery references after ownership validation, outside model history."""

        with self._connect() as connection:
            self._load(connection, context, self._head(connection, context))
            rows = connection.execute(
                "SELECT delivery FROM results WHERE tenant_id = %s AND conversation_id = %s "
                "AND run_id = %s AND delivery IS NOT NULL",
                self._key(context),
            ).fetchall()
            return tuple(ArtifactReference(**row["delivery"]) for row in rows)

    def history_page(self, context: RunContext, *, after: int = 0, limit: int = 100) -> HistoryPage:
        """Read a bounded journal page; asynchronous materialisation is a later group."""

        if not 1 <= limit <= 100 or after < 0:
            raise ValueError("invalid history page bounds")

        with self._connect() as connection:
            head = self._head(connection, context)
            rows = connection.execute(
                "SELECT sequence, payload FROM conversation_events WHERE tenant_id = %s "
                "AND conversation_id = %s AND sequence > %s ORDER BY sequence LIMIT %s",
                (*self._scope(context), after, limit),
            ).fetchall()
            next_cursor = rows[-1]["sequence"] if len(rows) == limit else head["cursor"]
            return HistoryPage(
                tuple(decode_item(row["payload"]) for row in rows),
                next_cursor,
                head["cursor"],
                head["deletion_generation"],
                self.recovery_id,
            )

    def _connect(self) -> Database:
        connection = Connection[dict[str, Any]].connect(
            self._dsn, row_factory=dict_row, target_session_attrs="read-write"
        )
        connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        return connection

    def _retry[T](self, transaction: Callable[[Database], T]) -> T:

        # Reconnection resolves the stable ID on the writer before applying a
        # missing operation. An uncertain earlier transaction is fenced by locks
        # and uniqueness. Retry only database work, never external effects.
        for _ in range(3):
            try:
                with self._connect() as connection:
                    result = transaction(connection)

                self._fault("after_commit")
                return result
            except psycopg.OperationalError, DeadlockDetected, SerializationFailure:
                continue
            except UniqueViolation:
                raise StateConflictError("durable identity already exists") from None

        raise CommitUnknownError("resolve the same stable ID on the primary before proceeding")

    def _write(self, connection: Database, sql: LiteralString, parameters: tuple[Any, ...]) -> int:
        cursor = connection.execute(sql, parameters)
        self._fault("after_write")
        return cursor.rowcount

    def _generation(self, connection: Database) -> None:

        # A shared generation lock precedes conversation/run/job locks. A recovery
        # cutover takes its exclusive lock while operators fence old applications.
        row = connection.execute("SELECT recovery_id FROM runtime_generation FOR SHARE").fetchone()

        if row is None or row["recovery_id"] != self.recovery_id:
            raise StateConflictError("database recovery generation changed")

    def _head(
        self, connection: Database, context: RunContext, *, allow_deleted: bool = False
    ) -> dict[str, Any]:
        self._generation(connection)
        row = connection.execute(
            "SELECT tenant_id, conversation_id, user_id, revision, admission_sequence, "
            "cursor, active_run_id, deletion_generation, deleted FROM conversations "
            "WHERE tenant_id = %s AND conversation_id = %s FOR UPDATE",
            self._scope(context),
        ).fetchone()

        if (
            row is None
            or row["user_id"] != context.user_id
            or (row["deleted"] and not allow_deleted)
        ):
            raise StateNotFoundError("conversation unavailable")

        row["recovery_id"] = self.recovery_id
        return row

    def _load(self, connection: Database, context: RunContext, head: dict[str, Any]) -> Checkpoint:
        row = connection.execute(
            "SELECT checkpoint FROM runs WHERE tenant_id = %s AND conversation_id = %s "
            "AND run_id = %s FOR UPDATE",
            self._key(context),
        ).fetchone()

        if row is None:
            raise StateNotFoundError("run unavailable")

        return self._response(connection, context, {"head": head, "run": row["checkpoint"]})

    def _response(
        self, connection: Database, context: RunContext, response: dict[str, Any]
    ) -> Checkpoint:
        data = response["run"]
        manifest_data = data.get("prompt_manifest")
        manifest = (
            PromptManifest(**{**manifest_data, "scope": PromptScope(**manifest_data["scope"])})
            if manifest_data
            else None
        )
        pending = tuple(
            PendingToolCall(call["call_id"], WaitReason(call["reason"]), call["reference_id"])
            for call in data["pending_calls"]
        )
        run = RunState(
            context,
            RunStatus(data["status"]),
            pending,
            data["revision"],
            data["model_steps"],
            data["tool_calls"],
            manifest,
        )
        return Checkpoint(self._conversation(connection, context, response["head"], run=True), run)

    def _conversation(
        self, connection: Database, context: RunContext, head: dict[str, Any], *, run: bool = False
    ) -> Conversation:

        # Sort by turn before journal sequence: a queued input can be journalled
        # before the previous turn's final answer. Limit before decoding; oversized
        # context fails explicitly until bounded context selection is implemented.
        rows = connection.execute(
            "SELECT e.payload FROM conversation_events e JOIN runs r USING "
            "(tenant_id, conversation_id, run_id) WHERE e.tenant_id = %s "
            "AND e.conversation_id = %s AND e.sequence <= %s "
            "AND (%s = false OR r.admission_sequence <= (SELECT admission_sequence FROM runs "
            "WHERE tenant_id = %s AND conversation_id = %s AND run_id = %s)) "
            "ORDER BY r.admission_sequence, e.sequence LIMIT 1001",
            (*self._scope(context), head["cursor"], run, *self._key(context)),
        ).fetchall()

        if len(rows) > 1000 or sum(len(str(row["payload"])) for row in rows) > 1048576:
            raise ValueError("context exceeds bound; use paginated history")

        return Conversation(
            context.tenant_id,
            context.user_id,
            context.conversation_id,
            tuple(decode_item(row["payload"]) for row in rows),
            head["revision"],
            head["active_run_id"],
            head["deletion_generation"],
            head["recovery_id"],
        )

    def _append(
        self,
        connection: Database,
        context: RunContext,
        head: dict[str, Any],
        encoded: list[dict[str, Any]],
        operation_id: str,
    ) -> None:
        if not encoded:
            head["cursor"] += 1

        for index, item in enumerate(encoded):
            head["cursor"] += 1
            self._write(
                connection,
                "INSERT INTO conversation_events VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    *self._key(context),
                    head["cursor"],
                    fingerprint([operation_id, index]),
                    Jsonb(item),
                ),
            )

        self._write(
            connection,
            "UPDATE conversations SET cursor = %s, last_activity = now() "
            "WHERE tenant_id = %s AND conversation_id = %s",
            (head["cursor"], *self._scope(context)),
        )

    def _dirty(self, connection: Database, context: RunContext, head: dict[str, Any]) -> None:
        self._write(
            connection,
            "INSERT INTO projection_work (tenant_id, conversation_id, dirty_cursor, "
            "deletion_generation, recovery_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, conversation_id) "
            "DO UPDATE SET dirty_cursor = EXCLUDED.dirty_cursor, "
            "deletion_generation = EXCLUDED.deletion_generation, recovery_id = "
            "EXCLUDED.recovery_id",
            (*self._scope(context), head["cursor"], head["deletion_generation"], self.recovery_id),
        )

    def _job(self, connection: Database, context: RunContext, revision: int, status: str) -> None:
        self._write(
            connection,
            "INSERT INTO jobs (tenant_id, conversation_id, run_id, job_id, operation, "
            "input_revision, status) "
            "VALUES (%s, %s, %s, %s, 'coordinator', %s, %s)",
            (*self._key(context), f"coordinator:{context.run_id}:{revision}", revision, status),
        )

    def _activate_next(self, connection: Database, context: RunContext) -> str | None:
        row = connection.execute(
            "SELECT run_id FROM runs WHERE tenant_id = %s AND conversation_id = %s "
            "AND status = 'queued' ORDER BY admission_sequence LIMIT 1 FOR UPDATE",
            self._scope(context),
        ).fetchone()

        if row is None:
            return None

        next_id: str = row["run_id"]
        self._write(
            connection,
            "UPDATE runs SET active = true WHERE tenant_id = %s AND conversation_id = %s AND "
            "run_id = %s",
            (*self._scope(context), next_id),
        )
        self._write(
            connection,
            "UPDATE jobs SET status = 'ready' WHERE tenant_id = %s AND conversation_id = %s "
            "AND run_id = %s AND status = 'blocked'",
            (*self._scope(context), next_id),
        )
        return next_id

    def _outcomes(
        self,
        connection: Database,
        context: RunContext,
        items: tuple[ConversationItem, ...],
        artifacts: tuple[ArtifactReference, ...],
    ) -> None:
        results = [
            item
            for item in items
            if isinstance(item, (VisibleResult, RestrictedReceipt, ToolFailure))
        ]
        delivery = {artifact.call_id: artifact for artifact in artifacts}

        if len(delivery) != len(artifacts) or not delivery.keys() <= {
            item.call_id for item in results
        }:
            raise ValueError("delivery requires a matching unique permitted outcome")

        if any(type(artifact) is not ArtifactReference for artifact in artifacts):
            raise ValueError("unsupported delivery metadata")

        for item in results:
            intent = connection.execute(
                "SELECT payload FROM conversation_events WHERE tenant_id = %s "
                "AND conversation_id = %s AND run_id = %s AND payload->>'type' = 'ToolCall' "
                "AND payload->'value'->>'call_id' = %s",
                (*self._key(context), item.call_id),
            ).fetchone()

            if intent is None:
                raise StateConflictError("outcome requires original tool intent")

            artifact = delivery.get(item.call_id)
            self._write(
                connection,
                "INSERT INTO results VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    *self._key(context),
                    item.call_id,
                    Jsonb(encode_items((item,))[0]),
                    Jsonb(plain(artifact)) if artifact else None,
                    fingerprint(intent["payload"]),
                ),
            )
            self._write(
                connection,
                "UPDATE jobs SET status = 'completed' WHERE tenant_id = %s AND conversation_id "
                "= %s "
                "AND run_id = %s AND job_id = %s",
                (*self._key(context), fingerprint(["tool", context.run_id, item.call_id])),
            )

    def _pending(
        self,
        connection: Database,
        context: RunContext,
        calls: tuple[PendingToolCall, ...],
        revision: int,
    ) -> None:
        for call in calls:
            if call.reason == WaitReason.JOB:
                self._write(
                    connection,
                    "INSERT INTO jobs (tenant_id, conversation_id, run_id, job_id, operation, "
                    "input_revision, status) "
                    "VALUES (%s, %s, %s, %s, 'synthetic_tool', %s, 'waiting') ON CONFLICT DO "
                    "NOTHING",
                    (
                        *self._key(context),
                        fingerprint(["tool", context.run_id, call.call_id]),
                        revision,
                    ),
                )
            else:
                rows = connection.execute(
                    "SELECT payload FROM conversation_events WHERE tenant_id = %s AND "
                    "conversation_id = %s "
                    "AND run_id = %s AND payload->>'type' = 'ToolCall' AND "
                    "payload->'value'->>'call_id' = %s",
                    (*self._key(context), call.call_id),
                ).fetchall()

                if len(rows) != 1 or not isinstance(decode_item(rows[0]["payload"]), ToolCall):
                    raise ValueError("approval requires one original tool intent")

                updated = self._write(
                    connection,
                    "INSERT INTO approvals (tenant_id, conversation_id, run_id, reference_id, "
                    "call_id, argument_fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT "
                    "(tenant_id, conversation_id, run_id, reference_id) DO UPDATE "
                    "SET reference_id = EXCLUDED.reference_id "
                    "WHERE approvals.call_id = EXCLUDED.call_id "
                    "AND approvals.argument_fingerprint = EXCLUDED.argument_fingerprint",
                    (
                        *self._key(context),
                        call.reference_id,
                        call.call_id,
                        fingerprint(rows[0]["payload"]),
                    ),
                )

                if updated != 1:
                    raise StateConflictError("approval reference reused with different intent")

    @staticmethod
    def _scope(context: RunContext) -> tuple[str, str]:
        return context.tenant_id, context.conversation_id

    @staticmethod
    def _key(context: RunContext) -> tuple[str, str, str]:
        return context.tenant_id, context.conversation_id, context.run_id
