"""Primary-only semantic memory reads, atomic publication and bounded retry accounting."""

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from agent_harness.memory.semantic import MemoryBlocked, MemoryFact
from agent_harness.memory.window import MemoryWindow
from agent_harness.prompts.resolution import PromptManifest, PromptScope
from agent_harness.runs.context import RunContext
from agent_harness.storage.codec import fingerprint, plain
from agent_harness.storage.interface import Checkpoint, StateConflictError
from agent_runtime.storage.postgres import Database, PostgresStateStore


def manifest(value: dict[str, Any]) -> PromptManifest:
    return PromptManifest(**{**value, "scope": PromptScope(**value["scope"])})


class SemanticStore:
    def __init__(self, store: PostgresStateStore) -> None:
        self.store = store

    def guard(self, connection: Database, checkpoint: Checkpoint) -> dict[str, Any]:
        context = checkpoint.run.context
        head = self.store._head(connection, context)
        row = connection.execute(
            "SELECT revision,admission_sequence,status FROM runs WHERE tenant_id=%s "
            "AND conversation_id=%s AND run_id=%s FOR UPDATE",
            self.store._key(context),
        ).fetchone()

        if (
            row is None
            or head["active_run_id"] != context.run_id
            or row["revision"] != checkpoint.run.revision
            or checkpoint.conversation.recovery_id != self.store.recovery_id
            or checkpoint.conversation.deletion_generation != head["deletion_generation"]
        ):
            raise StateConflictError("semantic memory execution scope changed")

        return row

    def read(self, checkpoint: Checkpoint, policy_key: str) -> MemoryWindow:
        """Load at most sixteen complete turns; an oversized turn blocks explicitly."""

        context = checkpoint.run.context

        with self.store._connect() as connection:
            current = self.guard(connection, checkpoint)
            prepared = connection.execute(
                "SELECT t.response FROM model_step_prompts s JOIN transitions t "
                "ON (s.tenant_id,s.operation_id)=(t.tenant_id,t.operation_id) "
                "WHERE s.tenant_id=%s AND s.conversation_id=%s AND s.run_id=%s "
                "AND s.step_number=%s",
                (*self.store._key(context), checkpoint.run.model_steps),
            ).fetchone()

            if prepared is None:
                raise StateConflictError("semantic memory requires a prepared model step")

            cursor = prepared["response"]["head"]["cursor"]
            versions = connection.execute(
                "SELECT COALESCE(max(version),0) AS version FROM semantic_summaries "
                "WHERE tenant_id=%s AND conversation_id=%s",
                self.store._scope(context),
            ).fetchone()
            assert versions is not None
            summary = connection.execute(
                "SELECT * FROM semantic_summaries WHERE tenant_id=%s AND conversation_id=%s "
                "AND recovery_id=%s AND deletion_generation=%s AND policy_key=%s "
                "AND (covered_turn<%s OR (covered_turn=%s AND partial_turn)) "
                "AND covered_cursor<=%s ORDER BY version DESC LIMIT 1",
                (
                    *self.store._scope(context),
                    self.store.recovery_id,
                    checkpoint.conversation.deletion_generation,
                    policy_key,
                    current["admission_sequence"],
                    current["admission_sequence"],
                    cursor,
                ),
            ).fetchone()
            covered = summary["covered_turn"] if summary else 0
            partial = summary["partial_turn"] if summary else False
            covered_cursor = summary["covered_cursor"] if summary else 0
            retained_cursor = summary["retained_cursor"] if summary else 0
            active = {
                "admission_sequence": current["admission_sequence"],
                "run_id": context.run_id,
                "status": current["status"],
                "events": connection.execute(
                    "SELECT sequence,payload FROM conversation_events WHERE tenant_id=%s "
                    "AND conversation_id=%s AND run_id=%s "
                    "AND (sequence>%s OR sequence=%s) AND sequence<=%s "
                    "ORDER BY sequence LIMIT 201",
                    (
                        *self.store._key(context),
                        covered_cursor
                        if partial and covered == current["admission_sequence"]
                        else 0,
                        retained_cursor
                        if partial and covered == current["admission_sequence"]
                        else 0,
                        cursor,
                    ),
                ).fetchall(),
            }

            if (
                len(active["events"]) > 200
                or sum(len(json.dumps(row["payload"]).encode()) for row in active["events"])
                > 262144
            ):
                raise MemoryBlocked("source_turn_limit")

            active.update(self.execution_metadata(connection, context, context.run_id))

            turns = connection.execute(
                "SELECT run_id,admission_sequence,status FROM runs "
                "WHERE tenant_id=%s AND conversation_id=%s "
                "AND (admission_sequence>%s OR (admission_sequence=%s AND %s)) "
                "AND admission_sequence<=%s ORDER BY admission_sequence LIMIT 17",
                (
                    *self.store._scope(context),
                    covered,
                    covered,
                    partial,
                    current["admission_sequence"],
                ),
            ).fetchall()

            # Never clip a call/result pair to fit a storage read. A 201st event
            # is a sentinel: the caller gets a fixed reason and dispatch stops.
            for turn in turns[:16]:
                if turn["run_id"] == context.run_id:

                    # The active turn already has the same prepared cursor and
                    # partial-summary boundary, with its size and evidence flags
                    # checked above. Reuse those rows while the conversation lock
                    # prevents event, approval or dispatch-state changes. For a
                    # fresh first turn this avoids reading its three sources twice.
                    turn.update(active)
                    continue

                events = connection.execute(
                    "SELECT sequence,payload FROM conversation_events WHERE tenant_id=%s "
                    "AND conversation_id=%s AND run_id=%s "
                    "AND (sequence>%s OR sequence=%s) AND sequence<=%s "
                    "ORDER BY sequence LIMIT 201",
                    (
                        *self.store._scope(context),
                        turn["run_id"],
                        covered_cursor if partial and covered == turn["admission_sequence"] else 0,
                        retained_cursor if partial and covered == turn["admission_sequence"] else 0,
                        cursor,
                    ),
                ).fetchall()

                if (
                    len(events) > 200
                    or sum(len(json.dumps(row["payload"]).encode()) for row in events) > 262144
                ):
                    raise MemoryBlocked("source_turn_limit")

                turn["events"] = events
                turn.update(self.execution_metadata(connection, context, turn["run_id"]))

            return MemoryWindow(
                checkpoint,
                cursor,
                current["admission_sequence"],
                versions["version"],
                covered,
                covered_cursor,
                tuple(MemoryFact(**value) for value in summary["facts"]) if summary else (),
                tuple(turns[:16]),
                active,
                len(turns) > 16,
                policy_key,
                partial,
                retained_cursor,
            )

    def execution_metadata(
        self,
        connection: Database,
        context: RunContext,
        run_id: str,
    ) -> dict[str, Any]:

        # Only fixed evidence flags leave these tables. Approval references,
        # delivery metadata, arguments and worker recovery payloads stay private.
        approved = connection.execute(
            "SELECT call_id FROM approvals WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s AND decision=true LIMIT 200",
            (*self.store._scope(context), run_id),
        ).fetchall()
        attempted = connection.execute(
            "SELECT call_id FROM jobs WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s AND call_id IS NOT NULL AND prepared_token>0 LIMIT 200",
            (*self.store._scope(context), run_id),
        ).fetchall()
        return {
            "approved": {row["call_id"] for row in approved},
            "attempted": {row["call_id"] for row in attempted},
        }

    def pending(self, window: MemoryWindow) -> dict[str, Any] | None:
        context = window.checkpoint.run.context

        with self.store._connect() as connection:
            self.guard(connection, window.checkpoint)
            return connection.execute(
                "SELECT * FROM semantic_operations WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s AND expected_version=%s AND policy_key=%s "
                "AND recovery_id=%s AND deletion_generation=%s "
                "AND operation_id LIKE %s "
                "AND result_version IS NULL ORDER BY target_turn LIMIT 1",
                (
                    *self.store._key(context),
                    window.version,
                    window.policy_key,
                    self.store.recovery_id,
                    window.checkpoint.conversation.deletion_generation,
                    f"summary:{window.checkpoint.run.model_steps}:%",
                ),
            ).fetchone()

    def reserve(
        self,
        window: MemoryWindow,
        *,
        target_turn: int,
        target_cursor: int,
        input_hash: str,
        prompt: PromptManifest,
        fence: Callable[[Database], object],
        partial_turn: bool = False,
        retained_cursor: int = 0,
    ) -> dict[str, Any]:
        """Pin a summary prompt and charge a dispatch before external inference.

        Two retries address the same operation, but each physical request consumes
        an attempt and the parent's existing worker budget. A lost commit reply
        reuses attempt_id inside _retry. Two competing owners cannot publish the
        older attempt after a newer reservation. No transaction spans model I/O.
        """

        context = window.checkpoint.run.context
        operation = f"summary:{window.checkpoint.run.model_steps}:" + fingerprint(
            [
                "semantic-v1",
                context.run_id,
                window.checkpoint.run.model_steps,
                window.version,
                target_turn,
                target_cursor,
                partial_turn,
                retained_cursor,
                window.policy_key,
                self.store.recovery_id,
                window.checkpoint.conversation.deletion_generation,
            ]
        )
        attempt_id = uuid4().hex

        def transaction(connection: Database) -> dict[str, Any]:
            self.guard(connection, window.checkpoint)
            fence(connection)
            connection.execute(
                "INSERT INTO semantic_operations (tenant_id,conversation_id,run_id,operation_id,"
                "expected_version,target_turn,target_cursor,recovery_id,deletion_generation,"
                "policy_key,prompt_manifest,input_fingerprint,partial_turn,retained_cursor) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    *self.store._key(context),
                    operation,
                    window.version,
                    target_turn,
                    target_cursor,
                    self.store.recovery_id,
                    window.checkpoint.conversation.deletion_generation,
                    window.policy_key,
                    Jsonb(plain(prompt)),
                    input_hash,
                    partial_turn,
                    retained_cursor,
                ),
            )
            row = connection.execute(
                "SELECT * FROM semantic_operations WHERE tenant_id=%s AND conversation_id=%s "
                "AND operation_id=%s FOR UPDATE",
                (*self.store._scope(context), operation),
            ).fetchone()
            assert row is not None

            if (
                row["recovery_id"] != self.store.recovery_id
                or row["deletion_generation"] != window.checkpoint.conversation.deletion_generation
                or row["policy_key"] != window.policy_key
            ):
                raise StateConflictError("semantic operation generation changed")

            if row["input_fingerprint"] != input_hash:
                raise StateConflictError("semantic operation input changed")

            if row["result_version"] is not None or row["last_attempt_id"] == attempt_id:
                return row

            if row["attempts"] >= 2:
                raise MemoryBlocked("summary_attempts_exhausted")

            charged = connection.execute(
                "UPDATE runs SET worker_attempts=worker_attempts+1 WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s AND worker_attempts<worker_budget",
                self.store._key(context),
            ).rowcount

            if not charged:
                raise MemoryBlocked("summary_run_budget_exhausted")

            updated = connection.execute(
                "UPDATE semantic_operations SET attempts=attempts+1,last_attempt_id=%s "
                "WHERE tenant_id=%s AND conversation_id=%s AND operation_id=%s RETURNING *",
                (attempt_id, *self.store._scope(context), operation),
            ).fetchone()
            assert updated is not None
            return updated

        return self.store._retry(transaction)

    def publish(
        self,
        window: MemoryWindow,
        operation: dict[str, Any],
        facts: tuple[MemoryFact, ...],
        *,
        fence: Callable[[Database], object],
        retirements: tuple[dict[str, object], ...] = (),
    ) -> int:
        """Commit content, provenance and coverage together under a version CAS.

        Example: versions 3 and 4 race to replace version 2. The first publication
        wins; the second observes version 3 and fails. Duplicate delivery of the
        same accepted result returns version 3. Deletion, restore, cancellation,
        lost worker ownership or a newer summary attempt reject stale publication.
        """

        context = window.checkpoint.run.context
        payload = plain(facts)

        def transaction(connection: Database) -> int:
            self.guard(connection, window.checkpoint)
            fence(connection)
            row = connection.execute(
                "SELECT * FROM semantic_operations WHERE tenant_id=%s AND conversation_id=%s "
                "AND operation_id=%s FOR UPDATE",
                (*self.store._scope(context), operation["operation_id"]),
            ).fetchone()
            assert row is not None

            if row["result_version"] is not None:
                saved = connection.execute(
                    "SELECT facts,retirements FROM semantic_summaries WHERE tenant_id=%s "
                    "AND conversation_id=%s AND version=%s",
                    (*self.store._scope(context), row["result_version"]),
                ).fetchone()

                if (
                    saved is None
                    or saved["facts"] != payload
                    or saved["retirements"] != list(retirements)
                ):
                    raise StateConflictError("conflicting semantic publication")

                return row["result_version"]

            latest = connection.execute(
                "SELECT COALESCE(max(version),0) AS version FROM semantic_summaries "
                "WHERE tenant_id=%s AND conversation_id=%s",
                self.store._scope(context),
            ).fetchone()
            assert latest is not None

            if (
                latest["version"] != window.version
                or row["last_attempt_id"] != operation["last_attempt_id"]
            ):
                raise StateConflictError("semantic summary version or attempt changed")

            version = window.version + 1
            self.store._write(
                connection,
                "INSERT INTO semantic_summaries (tenant_id,conversation_id,version,operation_id,"
                "recovery_id,deletion_generation,policy_key,covered_turn,covered_cursor,"
                "format_version,prompt_manifest,facts,partial_turn,retirements,retained_cursor) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,2,%s,%s,%s,%s,%s)",
                (
                    *self.store._scope(context),
                    version,
                    row["operation_id"],
                    self.store.recovery_id,
                    row["deletion_generation"],
                    row["policy_key"],
                    row["target_turn"],
                    row["target_cursor"],
                    Jsonb(row["prompt_manifest"]),
                    Jsonb(payload),
                    row["partial_turn"],
                    Jsonb(list(retirements)),
                    row["retained_cursor"],
                ),
            )
            self.store._write(
                connection,
                "UPDATE semantic_operations SET result_version=%s WHERE tenant_id=%s "
                "AND conversation_id=%s AND operation_id=%s",
                (version, *self.store._scope(context), row["operation_id"]),
            )
            return version

        return self.store._retry(transaction)
