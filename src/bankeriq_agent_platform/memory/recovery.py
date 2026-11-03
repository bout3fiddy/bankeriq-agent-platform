"""Durable, fenced single recovery for an explicitly rejected model context."""

from collections.abc import Callable

from agent_runtime.conversations.semantic import MemoryBlocked
from agent_runtime.storage.interface import Checkpoint, StateConflictError
from bankeriq_agent_platform.storage.postgres import Database

from .storage import SemanticStore


class ContextRecovery:
    def __init__(self, memory: SemanticStore) -> None:
        self.memory = memory
        self.store = memory.store

    def read(self, checkpoint: Checkpoint, policy: str):
        with self.store._connect() as connection:
            self.memory.guard(connection, checkpoint)
            row = self._read(connection, checkpoint)

            if row and (
                row["recovery_id"] != self.store.recovery_id
                or row["deletion_generation"] != checkpoint.conversation.deletion_generation
                or row["policy_key"] != policy
            ):
                raise StateConflictError("context recovery scope changed")

            return row

    def reject(self, checkpoint, policy, digest, tokens, *, fence):
        """Save a clean rejection before any recovery work or retry dispatch.

        The primary key is the prepared model step, independent of job delivery.
        For step 3, replaying reject() retains its original failed-input fingerprint.
        After retry_claimed becomes true, another rejection only records failure;
        it cannot reopen the allowance or replace the original attempt evidence.
        """

        def transaction(connection):
            self.memory.guard(connection, checkpoint)
            fence(connection)
            connection.execute(
                "INSERT INTO model_context_recoveries (tenant_id,conversation_id,run_id,"
                "step_number,recovery_id,deletion_generation,policy_key,request_fingerprint,"
                "input_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    *self.store._key(checkpoint.run.context),
                    checkpoint.run.model_steps,
                    self.store.recovery_id,
                    checkpoint.conversation.deletion_generation,
                    policy,
                    digest,
                    tokens,
                ),
            )
            connection.execute(
                "UPDATE model_context_recoveries SET failure_reason='retry_context_overflow' "
                "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                "AND step_number=%s AND retry_claimed",
                (*self.store._key(checkpoint.run.context), checkpoint.run.model_steps),
            )

        self.store._retry(transaction)

    def claim(
        self, checkpoint: Checkpoint, tokens: int, *, fence: Callable[[Database], object]
    ) -> None:
        """Spend the one retry before I/O, including the existing run attempt budget.

        A crash after this transaction conservatively spends the allowance. A new
        worker fails visibly rather than making a third model request. A timeout
        has unknown outcome and cannot be used to reset this durable decision.
        """

        def transaction(connection):
            self.memory.guard(connection, checkpoint)
            fence(connection)
            row = self._read(connection, checkpoint)

            if row is None or row["retry_claimed"]:
                raise MemoryBlocked("context_recovery_exhausted")

            if tokens >= row["input_tokens"]:
                raise MemoryBlocked("context_recovery_did_not_shrink")

            charged = connection.execute(
                "UPDATE runs SET worker_attempts=worker_attempts+1 WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s AND worker_attempts<worker_budget",
                self.store._key(checkpoint.run.context),
            ).rowcount

            if not charged:
                raise MemoryBlocked("context_recovery_budget_exhausted")

            connection.execute(
                "UPDATE model_context_recoveries SET retry_claimed=true WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s AND step_number=%s",
                (*self.store._key(checkpoint.run.context), checkpoint.run.model_steps),
            )

        # No automatic unknown-commit replay here: that could charge twice or
        # mistake a consumed allowance for permission to issue another request.
        with self.store._connect() as connection:
            transaction(connection)

    def _read(self, connection, checkpoint):
        return connection.execute(
            "SELECT * FROM model_context_recoveries WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s AND step_number=%s FOR UPDATE",
            (*self.store._key(checkpoint.run.context), checkpoint.run.model_steps),
        ).fetchone()
