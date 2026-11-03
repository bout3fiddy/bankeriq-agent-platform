"""Atomic fan-out and ordered joins using the existing worker ownership protocol."""

from psycopg.types.json import Jsonb

from agent_runtime.durable.handler import ContinuationHandler
from agent_runtime.prompts.resolution import PromptResolver
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import Checkpoint, StateConflictError
from agent_runtime.tools.results import ArtifactReference, ToolErrorCode, ToolFailure
from bankeriq_agent_platform.storage.codec import decode_item, encode_items, fingerprint, plain
from bankeriq_agent_platform.storage.postgres import Database, PostgresStateStore

from .queue import Lease, PostgresQueue
from .reasons import FailureReason
from .state import Completion, Recovery


class OrchestrationQueue(PostgresQueue):
    """Extend settlement; claims, crash accounting and operator controls remain shared.

    A model batch changes RUNNING revision 2 to WAITING revision 3 and installs
    every child in the same commit. Children keep revision 3 stable while finishing
    independently. The last result advances to RUNNING revision 4 and enrolls one
    continuation. Conversation locking serializes that decision across processes.
    """

    def __init__(self, store: PostgresStateStore, prompts: PromptResolver) -> None:
        super().__init__(store)
        self.prompts = prompts

    def _completion_change(self, lease: Lease, completion: Completion):
        if not completion.prepare_model:
            return super()._completion_change(lease, completion)

        return self.store.checkpoint_change(
            lease.context,
            expected_conversation_revision=lease.checkpoint.conversation.revision,
            expected_run_revision=lease.checkpoint.run.revision,
            expected_recovery_id=lease.checkpoint.conversation.recovery_id,
            expected_deletion_generation=lease.checkpoint.conversation.deletion_generation,
            status=RunStatus.RUNNING,
            model_steps=lease.checkpoint.run.model_steps + 1,
            prompt_resolver=self.prompts,
        )

    def _install_next(
        self, connection: Database, checkpoint: Checkpoint, completion: Completion
    ) -> None:
        if not completion.children:
            if completion.next_task:
                self._attach(connection, checkpoint, completion.next_task)

            return

        # Validate the entire batch before committing any dispatchable work. For
        # calls a/b, pending references and child positions must describe a/b in
        # exactly the same order. A malformed batch rolls back its intent too.
        calls = tuple(child.call for child in completion.children)

        if (
            completion.status != RunStatus.WAITING
            or completion.next_task is None
            or completion.items != calls
            or tuple(p.call_id for p in completion.pending) != tuple(c.call_id for c in calls)
            or len(calls) > 100
        ):
            raise ValueError("tool batch requires ordered intents, waits and continuation")

        context = checkpoint.run.context
        budget = connection.execute(
            "SELECT worker_budget FROM runs WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s",
            self.store._key(context),
        ).fetchone()

        if budget is None or budget["worker_budget"] != completion.next_task.run_budget:
            raise StateConflictError("group cannot change its parent's worker budget")

        group_id = f"group:{context.run_id}:{checkpoint.run.revision}"
        connection.execute(
            "INSERT INTO tool_groups VALUES (%s,%s,%s,%s,%s,%s,false)",
            (
                *self.store._key(context),
                group_id,
                completion.failure_policy,
                Jsonb(completion.next_task.encode()),
            ),
        )

        for position, child in enumerate(completion.children):
            state = child.recovery

            if state.phase != "ready" or state.run_budget != completion.next_task.run_budget:
                raise ValueError("children must retain the parent's budget and start ready")

            if child.resource_key is not None and not 1 <= len(child.resource_key) <= 200:
                raise ValueError("resource key must be bounded")

            # _pending already installed the scoped job with its stable call ID.
            # Enrollment and intent share this transaction, so no worker can see
            # a child whose model reply or parent ownership has not committed.
            connection.execute(
                "UPDATE jobs SET operation=%s, operation_version=%s, recovery=%s, "
                "input_fingerprint=%s, status='ready', group_id=%s, call_id=%s, "
                "call_position=%s, resource_key=%s WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s AND job_id=%s",
                (
                    state.kind,
                    state.version,
                    Jsonb(state.encode()),
                    fingerprint(state.encode()),
                    group_id,
                    child.call.call_id,
                    position,
                    child.resource_key,
                    *self.store._key(context),
                    fingerprint(["tool", context.run_id, child.call.call_id]),
                ),
            )

    def settle(
        self, lease: Lease, completion: Completion, *, reason: FailureReason = FailureReason.NONE
    ) -> Checkpoint:
        with self.store._connect() as connection:
            self.store._head(connection, lease.context)
            child = self._row(connection, lease.context, lease.job_id)["group_id"] is not None

        if not child:
            return super().settle(lease, completion, reason=reason)

        def transaction(connection: Database) -> Checkpoint:
            head = self.store._head(connection, lease.context)
            checkpoint = self.store._load(connection, lease.context, head)
            row = self._row(connection, lease.context, lease.job_id)
            digest = self._child_digest(completion, reason)

            if row["settlement_fingerprint"]:
                if row["settlement_fingerprint"] != digest:
                    raise StateConflictError("conflicting child outcome")

                return checkpoint

            self._owned(connection, lease)
            result = self._settle_child(connection, checkpoint, lease.job_id, completion, reason)

            # The result, sibling cancellation and join all roll back if ownership
            # expires while writing. Duplicate delivery never reaches the provider.
            valid = connection.execute(
                "SELECT 1 FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND lease_owner=%s AND ownership_token=%s AND lease_expiry>clock_timestamp()",
                self._identity(lease),
            ).fetchone()

            if valid is None:
                raise StateConflictError("child lease expired during settlement")

            return result

        return self.store._retry(transaction)

    @staticmethod
    def _child_digest(completion: Completion, reason: FailureReason) -> str:
        if (
            completion.next_task
            or completion.children
            or completion.pending
            or completion.prepare_model
            or completion.model_steps is not None
            or completion.tool_calls is not None
        ):
            raise ValueError("child cannot create independent continuation work")

        return fingerprint([encode_items(completion.items), plain(completion.artifacts), reason])

    def _settle_child(
        self,
        connection: Database,
        checkpoint: Checkpoint,
        job_id: str,
        completion: Completion,
        reason: FailureReason,
    ) -> Checkpoint:
        context = checkpoint.run.context
        row = self._row(connection, context, job_id)
        items = completion.items or (ToolFailure(row["call_id"], ToolErrorCode.EXECUTION_FAILED),)
        output = ContinuationHandler.child_outcome(
            checkpoint.run, row["call_id"], items, completion.artifacts
        )
        digest = self._child_digest(completion, reason)
        connection.execute(
            "UPDATE jobs SET status='completed', child_outcome=%s, child_delivery=%s, "
            "settlement_fingerprint=%s, effect_pending=false, failure_reason=%s, "
            "recovery=jsonb_set(recovery,'{phase}','\"complete\"') "
            "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
            (
                Jsonb(encode_items((output.model_result,))[0]),
                Jsonb(plain(output.artifact)) if output.artifact else None,
                digest,
                reason.value,
                *self.store._scope(context),
                job_id,
            ),
        )
        group = connection.execute(
            "SELECT * FROM tool_groups WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s AND group_id=%s FOR UPDATE",
            (*self.store._key(context), row["group_id"]),
        ).fetchone()
        assert group is not None
        failed_fast = isinstance(output.model_result, ToolFailure) and group["failure_policy"] == (
            "fail_fast"
        )

        if failed_fast:

            # Unknown sibling effects remain explicit. Fencing precedes terminal
            # checkpoint publication; late replies cannot create another join.
            connection.execute(
                "UPDATE jobs SET status='completed', ownership_token=ownership_token+1 "
                "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s AND group_id=%s "
                "AND settlement_fingerprint IS NULL",
                (*self.store._key(context), row["group_id"]),
            )

        children = connection.execute(
            "SELECT * FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
            "AND group_id=%s ORDER BY call_position",
            (*self.store._key(context), row["group_id"]),
        ).fetchall()

        if not failed_fast and any(child["child_outcome"] is None for child in children):
            return checkpoint

        if group["joined"]:
            raise StateConflictError("group already joined")

        outcomes = tuple(
            decode_item(child["child_outcome"])
            for child in children
            if child["child_outcome"] is not None
        )
        artifacts = tuple(
            ArtifactReference(**child["child_delivery"])
            for child in children
            if child["child_delivery"] is not None
        )
        result = self.store.checkpoint_change(
            context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            expected_recovery_id=checkpoint.conversation.recovery_id,
            expected_deletion_generation=checkpoint.conversation.deletion_generation,
            status=RunStatus.FAILED if failed_fast else RunStatus.RUNNING,
            items=outcomes,
            artifacts=artifacts,
        )(connection)
        connection.execute(
            "UPDATE tool_groups SET joined=true WHERE tenant_id=%s AND conversation_id=%s "
            "AND run_id=%s AND group_id=%s",
            (*self.store._key(context), row["group_id"]),
        )

        if failed_fast:
            connection.execute(
                "UPDATE jobs SET status=CASE WHEN effect_pending THEN 'cancelled_unknown' "
                "ELSE 'cancelled' END, failure_reason=CASE WHEN effect_pending "
                "THEN 'unknown_effect' ELSE 'cancelled' END WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s AND group_id=%s "
                "AND settlement_fingerprint IS NULL",
                (*self.store._key(context), row["group_id"]),
            )
        else:
            self._attach(connection, result, Recovery.decode(group["continuation"]))

        return result

    def _finish_exhausted(
        self,
        connection: Database,
        context: RunContext,
        job_id: str,
        *,
        approval: bool = False,
        reason: FailureReason = FailureReason.ATTEMPTS_EXHAUSTED,
    ) -> None:
        row = self._row(connection, context, job_id)

        if row["group_id"] is None or row["effect_pending"]:
            super()._finish_exhausted(connection, context, job_id, approval=approval, reason=reason)
            return

        if row["crash_count"] >= Recovery.decode(row["recovery"]).attempt_limit:
            reason = FailureReason.CRASH_LIMIT

        checkpoint = self.store._load(connection, context, self.store._head(connection, context))
        self._settle_child(
            connection,
            checkpoint,
            job_id,
            Completion(RunStatus.FAILED),
            FailureReason.APPROVAL_EXPIRED if approval else reason,
        )
