"""Primary-only leases and atomic settlement over the existing journal/job records."""

from dataclasses import dataclass, replace
from math import ceil
from time import monotonic
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from agent_harness.conversations.conversation import ConversationItem
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import TERMINAL_STATUSES, RunStatus
from agent_harness.storage.codec import fingerprint, plain
from agent_harness.storage.interface import Checkpoint, StateConflictError, StateNotFoundError
from agent_runtime.storage.postgres import Database, PostgresStateStore
from agent_runtime.workers.reasons import FailureReason
from agent_runtime.workers.releases import check_admission
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery, replay_safe


@dataclass(frozen=True)
class Lease:
    context: RunContext
    job_id: str
    owner: str
    token: int
    checkpoint: Checkpoint
    state: Recovery
    attempt: int
    effect_pending: bool = False
    queue_wait_ms: int | None = None
    queue_wait_upper_ms: int | None = None


class ApprovalExpiredError(StateConflictError):
    """An approval expired at the final database-time dispatch check."""


class PostgresQueue:
    """Share the store's primary routing, generation checks and transaction retries.

    attach() enrolls a synthetic coordinator job created by admission. Existing
    synchronous profiles keep recovery=NULL and are never claimed by this adapter.
    Each lease uses a fresh owner identity, so a lost claim acknowledgement can be
    resolved without reserving a second job. Locks end before handlers execute.
    """

    def __init__(self, store: PostgresStateStore) -> None:
        self.store = store

    def configure(self, queue: str, *, concurrency: int, tenant_concurrency: int) -> None:
        if concurrency < 1 or tenant_concurrency < 1:
            raise ValueError("worker limits must be positive")

        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO worker_limits VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (queue, concurrency, tenant_concurrency),
            )
            row = connection.execute(
                "SELECT * FROM worker_limits WHERE queue=%s", (queue,)
            ).fetchone()

            if row != dict(
                queue=queue, concurrency=concurrency, tenant_concurrency=tenant_concurrency
            ):
                raise StateConflictError("worker limits differ from the registered queue")

    def admit(
        self,
        context: RunContext,
        *,
        request_id: str,
        items: tuple[ConversationItem, ...],
        state: Recovery,
    ) -> Checkpoint:
        """Accept input and registered recovery state in one primary transaction."""

        state.encode()
        change = self.store.admission_change(context, request_id=request_id, items=items)

        def transaction(connection: Database) -> Checkpoint:
            self.store._head(connection, context)
            check_admission(connection, context, request_id)
            checkpoint = change(connection)

            # A retry after progression still identifies the original admission
            # job at revision zero. Its immutable fingerprint detects changed
            # handler arguments independently of the user-message fingerprint.
            original = f"coordinator:{context.run_id}:0"
            row = self._row(connection, context, original)

            if row["input_fingerprint"]:
                if row["input_fingerprint"] != fingerprint(state.encode()):
                    raise StateConflictError("admission recovery state conflicts")
            else:
                self._attach(connection, checkpoint, state)

            return checkpoint

        return self.store._retry(transaction)

    def attach(self, context: RunContext, state: Recovery) -> str:
        state.encode()

        def transaction(connection: Database) -> str:
            head = self.store._head(connection, context)
            checkpoint = self.store._load(connection, context, head)
            return self._attach(connection, checkpoint, state)

        return self.store._retry(transaction)

    def claim(self, queue: str, handlers: Handlers, *, seconds: float = 30) -> Lease | None:
        if not 0 < seconds <= 3600:
            raise ValueError("lease duration must be in (0, 3600]")

        self.recover_expired(queue)
        versions = [version for kind, version in handlers.entries if kind == queue]

        if not versions:
            return None

        owner = str(uuid4())
        sampled_at: float | None = None

        def transaction(connection: Database) -> Lease | None:
            nonlocal sampled_at

            self.store._generation(connection)

            # A single short admission lock makes queue and tenant counts exact
            # across processes. It is never held during execution or settlement.
            # SKIP LOCKED below prevents one busy conversation blocking the scan.
            # These reads all depend on the admission lock, and none depends on
            # another read's result. Submit them together in their original order.
            # For example, concurrent workers still serialize their capacity check
            # before either leases a job, but avoid four separate network exchanges.
            # A recovered ownership ID is checked before using the capacity count.
            with connection.pipeline():
                connection.execute("SELECT pg_advisory_xact_lock(720322)")
                limits_cursor = connection.execute(
                    "SELECT * FROM worker_limits WHERE queue=%s", (queue,)
                )
                previous_cursor = connection.execute(
                    "SELECT * FROM jobs WHERE lease_owner=%s", (owner,)
                )
                count_cursor = connection.execute(
                    "SELECT count(*) AS n FROM jobs WHERE operation=%s AND status='leased' "
                    "AND lease_expiry>clock_timestamp()",
                    (queue,),
                )

            limits = limits_cursor.fetchone()

            if limits is None:
                raise ValueError("queue limits must be configured before claiming")

            previous = previous_cursor.fetchone()

            if previous:
                return self._recover_claim(connection, previous)

            count = count_cursor.fetchone()
            assert count is not None

            if count["n"] >= limits["concurrency"]:
                return None

            sampled_at = monotonic()
            candidates = connection.execute(
                "SELECT j.*, c.user_id,extract(epoch FROM (clock_timestamp()-j.due_at))*1000 "
                "AS queue_wait_ms FROM jobs j JOIN conversations c USING "
                "(tenant_id,conversation_id) JOIN runs r USING (tenant_id,conversation_id,run_id) "
                "WHERE j.operation=%s AND j.operation_version=ANY(%s) "
                "AND j.recovery->'arguments' @> (%s::jsonb->j.operation_version::text) "
                "AND j.recovery IS NOT NULL AND NOT c.deleted "
                "AND (SELECT count(*) FROM jobs busy WHERE busy.tenant_id=j.tenant_id "
                "AND busy.operation=j.operation AND busy.status='leased' "
                "AND busy.lease_expiry>clock_timestamp()) < %s "
                "AND r.active AND c.active_run_id=j.run_id "
                "AND j.status IN ('ready','retry','reconcile','approval') "
                "AND j.due_at<=clock_timestamp() "
                "AND (j.resource_key IS NULL OR NOT EXISTS (SELECT 1 FROM jobs busy "
                "WHERE busy.tenant_id=j.tenant_id AND busy.resource_key=j.resource_key "
                "AND busy.job_id<>j.job_id AND (busy.effect_pending OR "
                "(busy.status='leased' AND busy.lease_expiry>clock_timestamp())))) "
                "ORDER BY j.due_at,j.job_id LIMIT 64 FOR UPDATE OF c SKIP LOCKED",
                (
                    queue,
                    versions,
                    Jsonb(handlers.requirements(queue)),
                    limits["tenant_concurrency"],
                ),
            ).fetchall()

            for candidate in candidates:
                context = RunContext(
                    candidate["tenant_id"],
                    candidate["user_id"],
                    candidate["conversation_id"],
                    candidate["run_id"],
                )
                head = connection.execute(
                    "SELECT 1 FROM conversations WHERE tenant_id=%s AND conversation_id=%s "
                    "FOR UPDATE SKIP LOCKED",
                    self.store._scope(context),
                ).fetchone()

                if head is None:
                    continue

                checkpoint = self.store._load(
                    connection, context, self.store._head(connection, context)
                )

                if checkpoint.conversation.active_run_id != context.run_id:
                    continue

                row = self._row(connection, context, candidate["job_id"])
                eligible = connection.execute(
                    "SELECT (status IN ('ready','retry','approval','reconcile') "
                    "AND due_at<=clock_timestamp()) AS ok FROM jobs "
                    "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
                    (*self.store._scope(context), row["job_id"]),
                ).fetchone()

                if eligible is None or not eligible["ok"]:
                    continue

                state = Recovery.decode(row["recovery"])

                if (state.kind, state.version) != (row["operation"], row["operation_version"]):
                    raise StateConflictError("recovery registration differs from job metadata")

                # Unknown versions stay durable and unclaimed for a compatible
                # deployment. No caller-supplied module name is ever imported.
                try:
                    handler = handlers.resolve(state)
                except ValueError:
                    continue

                tenant_count = connection.execute(
                    "SELECT count(*) AS n FROM jobs WHERE tenant_id=%s AND operation=%s "
                    "AND status='leased' AND lease_expiry>clock_timestamp()",
                    (context.tenant_id, queue),
                ).fetchone()
                assert tenant_count is not None

                if tenant_count["n"] >= limits["tenant_concurrency"]:
                    continue

                if state.phase == "reconcile" and handler.reconcile is None:
                    self._stop(connection, context, row["job_id"], FailureReason.LOOKUP_UNAVAILABLE)
                    continue

                if (
                    row["effect_pending"]
                    and state.phase != "reconcile"
                    and not replay_safe(state, handler)
                ):
                    self._park(connection, context, row["job_id"], state, "reconcile")
                    continue

                if row["status"] == "approval":
                    self._finish_exhausted(connection, context, row["job_id"], approval=True)
                    continue

                if row["attempt"] >= state.attempt_limit and state.phase != "reconcile":
                    self._finish_exhausted(connection, context, row["job_id"])
                    continue

                budget = connection.execute(
                    "UPDATE runs SET worker_attempts=worker_attempts+1 WHERE tenant_id=%s "
                    "AND conversation_id=%s AND run_id=%s AND worker_attempts<worker_budget",
                    self.store._key(context),
                ).rowcount

                if not budget:
                    self._finish_exhausted(
                        connection,
                        context,
                        row["job_id"],
                        reason=FailureReason.RUN_BUDGET_EXHAUSTED,
                    )
                    continue

                connection.execute(
                    "UPDATE jobs SET status='leased', attempt=attempt+1, lease_owner=%s, "
                    "lease_expiry=clock_timestamp()+%s*interval '1 second', "
                    "ownership_token=ownership_token+1, failure_reason='none' WHERE tenant_id=%s "
                    "AND conversation_id=%s AND job_id=%s",
                    (owner, seconds, *self.store._scope(context), row["job_id"]),
                )
                return Lease(
                    context,
                    row["job_id"],
                    owner,
                    row["ownership_token"] + 1,
                    checkpoint,
                    state,
                    row["attempt"] + 1,
                    row["effect_pending"],
                    queue_wait_ms=max(0, round(candidate["queue_wait_ms"])),
                )

            return None

        lease = self.store._retry(transaction)

        if lease is not None and lease.queue_wait_ms is not None:
            assert sampled_at is not None

            # Candidate age precedes ownership writes and commit acknowledgement.
            # Add the entire interval from before that SELECT through the returned
            # commit, producing a conservative due-to-claim upper bound. For
            # example, age 100 ms plus a 40-ms SELECT/claim interval bounds the
            # actual wait by 141 ms, including one ms for rounded source age.
            # This can include capacity/resource/ordering waits before eligibility.
            # A recovered unknown-ack claim has no sample and remains unmeasured.
            lease = replace(
                lease,
                queue_wait_upper_ms=(
                    lease.queue_wait_ms + ceil((monotonic() - sampled_at) * 1000) + 1
                ),
            )

        return lease

    def recover_expired(self, queue: str) -> None:
        """Account for lost processes even when this deployment lacks their handler."""

        def transaction(connection: Database) -> None:
            self.store._generation(connection)
            rows = connection.execute(
                "SELECT j.tenant_id,j.conversation_id,j.run_id,j.job_id,c.user_id "
                "FROM jobs j JOIN conversations c USING (tenant_id,conversation_id) "
                "WHERE operation=%s AND recovery IS NOT NULL AND status='leased' "
                "AND lease_expiry<=clock_timestamp() "
                "AND NOT c.deleted ORDER BY lease_expiry LIMIT 64 FOR UPDATE OF c SKIP LOCKED",
                (queue,),
            ).fetchall()

            for candidate in rows:
                context = RunContext(
                    candidate["tenant_id"],
                    candidate["user_id"],
                    candidate["conversation_id"],
                    candidate["run_id"],
                )
                head = self.store._head(connection, context)
                self.store._load(connection, context, head)
                row = self._row(connection, context, candidate["job_id"])

                # A heartbeat may have won the job lock after the candidate read.
                # Evaluate expiry again under that lock using database wall time.
                updated = connection.execute(
                    "UPDATE jobs SET status='ready', lease_expiry=NULL, crash_count=crash_count+1, "
                    "failure_reason='process_lost' WHERE tenant_id=%s AND conversation_id=%s "
                    "AND job_id=%s AND status='leased' AND lease_expiry<=clock_timestamp()",
                    (*self.store._scope(context), row["job_id"]),
                ).rowcount

                if not updated:
                    continue

                state = Recovery.decode(row["recovery"])

                if row["attempt"] >= state.attempt_limit:
                    self._finish_exhausted(connection, context, row["job_id"])
                else:
                    budget = connection.execute(
                        "SELECT worker_attempts>=worker_budget AS exhausted FROM runs "
                        "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s",
                        self.store._key(context),
                    ).fetchone()
                    assert budget is not None

                    if budget["exhausted"]:
                        self._finish_exhausted(
                            connection,
                            context,
                            row["job_id"],
                            reason=FailureReason.RUN_BUDGET_EXHAUSTED,
                        )

        self.store._retry(transaction)

    def release(self, lease: Lease) -> None:
        """Refund an undispatched claim on orderly shutdown, once per ownership token."""

        self._timeline(lease)

        def transaction(connection: Database) -> None:
            head = self.store._head(connection, lease.context)
            self.store._load(connection, lease.context, head)
            row = self._row(connection, lease.context, lease.job_id)

            if row["released_token"] == lease.token:
                return

            self._fence(connection, lease)

            # Claim 4 reserves attempt 2, but shutdown can arrive before dispatch.
            # Refund only when prepare/lookup never used token 4. A process that
            # already contacted a provider must settle, drain, or expire normally.
            if row["prepared_token"] == lease.token:
                raise StateConflictError("prepared work cannot refund its execution allowance")

            connection.execute(
                "UPDATE jobs SET status='ready', attempt=attempt-1, released_token=%s, "
                "lease_expiry=NULL, failure_reason='none' WHERE tenant_id=%s "
                "AND conversation_id=%s AND job_id=%s",
                (lease.token, *self._key(lease)),
            )
            connection.execute(
                "UPDATE runs SET worker_attempts=worker_attempts-1 WHERE tenant_id=%s "
                "AND conversation_id=%s AND run_id=%s",
                self.store._key(lease.context),
            )

        self.store._retry(transaction)

    def begin_lookup(self, lease: Lease) -> None:
        """Reserve the lookup boundary so draining cannot refund an in-flight lookup."""

        def transaction(connection: Database) -> None:
            row = self._owned(connection, lease)

            if Recovery.decode(row["recovery"]).phase != "reconcile":
                raise StateConflictError("lookup requires reconciliation phase")

            if not self._approval_current(connection, lease.context, lease.job_id):
                raise ApprovalExpiredError("approval expired before lookup")

            connection.execute(
                "UPDATE jobs SET prepared_token=ownership_token WHERE tenant_id=%s "
                "AND conversation_id=%s AND job_id=%s",
                self._key(lease),
            )

        self.store._retry(transaction)

    def reject(self, lease: Lease, reason: FailureReason) -> None:
        """Retain unknown effects when access or approval no longer permits execution."""

        def transaction(connection: Database) -> bool:
            row = self._owned(connection, lease)

            if row["effect_pending"]:
                self._stop(connection, lease.context, lease.job_id, reason)
                return True

            return False

        if not self.store._retry(transaction):
            self.settle(lease, Completion(RunStatus.FAILED), reason=reason)

    def approval_granted(self, lease: Lease) -> bool:
        with self.store._connect() as connection:
            row = self._owned(connection, lease)
            return row["approval_fingerprint"] is not None

    def approval_current(self, lease: Lease) -> bool:
        with self.store._connect() as connection:
            self._owned(connection, lease)
            return self._approval_current(connection, lease.context, lease.job_id)

    def renew(self, lease: Lease, *, seconds: float = 30) -> None:
        if not 0 < seconds <= 3600:
            raise ValueError("invalid lease duration")

        def transaction(connection: Database) -> None:
            self.store._generation(connection)
            self._timeline(lease)

            # Renewal touches only its job. Settlement takes conversation/run/job
            # locks; renewal never requests their locks in the reverse order.
            updated = connection.execute(
                "UPDATE jobs SET lease_expiry=clock_timestamp()+%s*interval '1 second' "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND lease_owner=%s AND ownership_token=%s AND status='leased' "
                "AND lease_expiry>clock_timestamp()",
                (seconds, *self._identity(lease)),
            ).rowcount

            if updated != 1:
                raise StateConflictError("expired or stale lease")

        self.store._retry(transaction)

    def prepare(self, lease: Lease) -> Recovery:
        """Save exact final arguments and dispatch phase before any external call."""

        def transaction(connection: Database) -> Recovery:
            row = self._owned(connection, lease)
            state = Recovery.decode(row["recovery"])

            if state.phase == "reconcile":
                raise StateConflictError("reconciliation cannot dispatch an operation")

            if not self._approval_current(connection, lease.context, lease.job_id):
                raise ApprovalExpiredError("approval expired before dispatch")

            prepared = state.model_copy(update={"phase": "dispatch"})
            connection.execute(
                "UPDATE jobs SET recovery=%s, effect_pending=true, prepared_token=ownership_token "
                "WHERE tenant_id=%s AND conversation_id=%s "
                "AND job_id=%s",
                (Jsonb(prepared.encode()), *self._key(lease)),
            )
            return prepared

        return self.store._retry(transaction)

    def settle(
        self, lease: Lease, completion: Completion, *, reason: FailureReason = FailureReason.NONE
    ) -> Checkpoint:
        """Fence ownership, then reuse the journal's atomic checkpoint transition."""

        payload = plain(completion)
        payload["next_task"] = completion.next_task.encode() if completion.next_task else None
        payload["children"] = [
            {
                "call": plain(child.call),
                "recovery": child.recovery.encode(),
                "resource_key": child.resource_key,
            }
            for child in completion.children
        ]

        if reason != FailureReason.NONE:
            payload["failure_reason"] = reason.value

        digest = fingerprint(payload)
        change = self._completion_change(lease, completion)

        if completion.next_task:
            completion.next_task.encode()

        def transaction(connection: Database) -> Checkpoint:
            head = self.store._head(connection, lease.context)
            self.store._load(connection, lease.context, head)
            row = self._row(connection, lease.context, lease.job_id)

            # Lost acknowledgement: identical bytes resolve the original saved
            # response on the primary. A conflicting result never overwrites it.
            if row["settlement_fingerprint"]:
                if row["settlement_fingerprint"] != digest:
                    raise StateConflictError("conflicting settlement identity")

                return self.store._response(connection, lease.context, row["settlement_response"])

            self._fence(connection, lease)

            if completion.status not in TERMINAL_STATUSES and completion.next_task is None:
                raise ValueError("nonterminal settlement requires registered next work")

            connection.execute(
                "UPDATE jobs SET status='completed' WHERE tenant_id=%s AND conversation_id=%s "
                "AND job_id=%s",
                self._key(lease),
            )
            checkpoint = change(connection)
            self.store._fault("worker_after_output")

            self._install_next(connection, checkpoint, completion)

            self.store._fault("worker_after_next")
            response = connection.execute(
                "SELECT response FROM transitions WHERE tenant_id=%s AND operation_id=%s",
                (
                    lease.context.tenant_id,
                    f"checkpoint:{lease.context.run_id}:{lease.checkpoint.run.revision}",
                ),
            ).fetchone()
            assert response is not None

            # Check database time again after all writes. A transaction delayed
            # past its lease rolls back its output and continuation together.
            updated = connection.execute(
                "UPDATE jobs SET settlement_fingerprint=%s, settlement_response=%s, "
                "effect_pending=false, failure_reason=%s, "
                "recovery=jsonb_set(recovery,'{phase}','\"complete\"') "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND lease_owner=%s AND ownership_token=%s AND lease_expiry>clock_timestamp()",
                (digest, Jsonb(response["response"]), reason.value, *self._identity(lease)),
            ).rowcount

            if updated != 1:
                raise StateConflictError("lease expired during settlement")

            self.store._fault("worker_after_settlement")
            return checkpoint

        return self.store._retry(transaction)

    def _completion_change(self, lease: Lease, completion: Completion):
        return self.store.checkpoint_change(
            lease.context,
            expected_conversation_revision=lease.checkpoint.conversation.revision,
            expected_run_revision=lease.checkpoint.run.revision,
            expected_recovery_id=lease.checkpoint.conversation.recovery_id,
            expected_deletion_generation=lease.checkpoint.conversation.deletion_generation,
            status=completion.status,
            items=completion.items,
            pending_calls=completion.pending,
            artifacts=completion.artifacts,
            model_steps=completion.model_steps,
            tool_calls=completion.tool_calls,
        )

    def _install_next(
        self, connection: Database, checkpoint: Checkpoint, completion: Completion
    ) -> None:
        if completion.children or completion.prepare_model:
            raise ValueError("asynchronous completion requires the orchestration queue")

        if completion.next_task:
            self._attach(connection, checkpoint, completion.next_task)

    def pause(
        self,
        lease: Lease,
        *,
        seconds: float,
        approval: bool = False,
        handler: Handler | None = None,
    ) -> None:
        """Save a retry deadline or a bounded approval wait without holding a worker slot."""

        if not 0 <= seconds <= 86400 * 30:
            raise ValueError("wait duration outside supported bound")

        self._timeline(lease)
        digest = fingerprint([self._identity(lease), seconds, approval])

        def transaction(connection: Database) -> None:
            self.store._head(connection, lease.context)
            row = self._row(connection, lease.context, lease.job_id)

            if row["control_fingerprint"] == digest:
                return

            row = self._owned(connection, lease)
            state = Recovery.decode(row["recovery"])

            if state.phase in ("dispatch", "reconcile") and (
                handler is None or not replay_safe(state, handler)
            ):
                raise StateConflictError("unknown effect requires safe replay or reconciliation")

            if approval and row["approval_fingerprint"] is not None:
                raise StateConflictError("approval is already bound to this operation")

            phase = "approval" if approval else "retry"
            connection.execute(
                "UPDATE jobs SET status=%s, recovery=%s, control_fingerprint=%s, "
                "failure_reason=%s, "
                "due_at=clock_timestamp()+%s*interval '1 second', "
                "wait_expiry=CASE WHEN %s THEN clock_timestamp()+%s*interval '1 second' "
                "ELSE wait_expiry END "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
                (
                    phase,
                    Jsonb(state.model_copy(update={"phase": phase}).encode()),
                    digest,
                    "none" if approval else "execution_retry",
                    seconds,
                    approval,
                    seconds,
                    *self._key(lease),
                ),
            )

        self.store._retry(transaction)

    def reconcile(self, lease: Lease) -> None:
        self._timeline(lease)

        def transaction(connection: Database) -> None:
            head = self.store._head(connection, lease.context)
            self.store._load(connection, lease.context, head)
            row = self._row(connection, lease.context, lease.job_id)

            # A lost acknowledgement can revisit the already parked operation.
            # Only the same ownership identity resolves this control transition;
            # a newer claimant cannot be overwritten by an older process.
            if (
                row["status"] == "reconcile"
                and row["lease_owner"] == lease.owner
                and row["ownership_token"] == lease.token
            ):
                return

            row = self._owned(connection, lease)
            self._park(
                connection,
                lease.context,
                lease.job_id,
                Recovery.decode(row["recovery"]),
                "reconcile",
            )

        self.store._retry(transaction)

    def approve(self, context: RunContext, job_id: str, expected: Recovery) -> None:
        """Trusted approval binds exact saved version, identity, arguments and expiry."""

        digest = fingerprint(expected.encode())

        def transaction(connection: Database) -> None:
            self.store._head(connection, context)
            row = self._row(connection, context, job_id)

            if row["approval_fingerprint"]:
                if row["approval_fingerprint"] != digest:
                    raise StateConflictError("conflicting approval decision")

                return

            saved = Recovery.decode(row["recovery"])

            if saved.model_copy(update={"phase": "ready"}) != expected:
                raise StateConflictError("approval does not match saved intent")

            updated = connection.execute(
                "UPDATE jobs SET status='ready', due_at=clock_timestamp(), recovery=%s, "
                "approval_fingerprint=%s "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND status='approval' AND wait_expiry>clock_timestamp()",
                (Jsonb(expected.encode()), digest, *self.store._scope(context), job_id),
            ).rowcount

            if updated != 1:
                raise StateConflictError("approval expired or already consumed")

        self.store._retry(transaction)

    def cancel(self, context: RunContext) -> Checkpoint:
        """Fence current and future ownership while committing run cancellation."""

        def transaction(connection: Database) -> Checkpoint:
            head = self.store._head(connection, context)
            checkpoint = self.store._load(connection, context, head)

            if checkpoint.run.status == RunStatus.CANCELLED:
                return checkpoint

            connection.execute(
                "UPDATE jobs SET status='completed', ownership_token=ownership_token+1 "
                "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                "AND recovery IS NOT NULL AND status<>'completed'",
                self.store._key(context),
            )
            result = self.store.checkpoint_change(
                context,
                expected_conversation_revision=checkpoint.conversation.revision,
                expected_run_revision=checkpoint.run.revision,
                status=RunStatus.CANCELLED,
                expected_recovery_id=checkpoint.conversation.recovery_id,
                expected_deletion_generation=checkpoint.conversation.deletion_generation,
            )(connection)

            # Cancellation cannot undo a call already dispatched. Retain an
            # explicit unknown-effect record for provider lookup/operator review;
            # no cancelled job becomes eligible for automatic execution again.
            connection.execute(
                "UPDATE jobs SET status=CASE WHEN effect_pending "
                "THEN 'cancelled_unknown' ELSE 'cancelled' END, "
                "failure_reason=CASE WHEN effect_pending THEN 'unknown_effect' "
                "ELSE 'cancelled' END "
                "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                "AND recovery IS NOT NULL AND settlement_fingerprint IS NULL",
                self.store._key(context),
            )
            return result

        return self.store._retry(transaction)

    def _attach(self, connection: Database, checkpoint: Checkpoint, state: Recovery) -> str:
        if state.phase != "ready":
            raise ValueError("new tasks must begin in ready phase")

        context = checkpoint.run.context
        job_id = f"coordinator:{context.run_id}:{checkpoint.run.revision}"
        row = self._row(connection, context, job_id)
        digest = fingerprint(state.encode())

        if row["input_fingerprint"]:
            if row["input_fingerprint"] != digest:
                raise StateConflictError("job identity reused with different input")

            return job_id

        if row["status"] not in ("ready", "blocked", "waiting"):
            raise StateConflictError("job is no longer available for worker registration")

        connection.execute(
            "UPDATE runs SET worker_budget=COALESCE(worker_budget,%s) WHERE tenant_id=%s "
            "AND conversation_id=%s AND run_id=%s",
            (state.run_budget, *self.store._key(context)),
        )
        budget = connection.execute(
            "SELECT worker_budget FROM runs WHERE tenant_id=%s "
            "AND conversation_id=%s AND run_id=%s",
            self.store._key(context),
        ).fetchone()

        if budget is None or budget["worker_budget"] != state.run_budget:
            raise StateConflictError("run budget cannot change across tasks")

        connection.execute(
            "UPDATE jobs SET recovery=%s, input_fingerprint=%s, operation=%s, "
            "operation_version=%s, "
            "status=CASE WHEN status='waiting' THEN 'ready' ELSE status END "
            "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
            (
                Jsonb(state.encode()),
                digest,
                state.kind,
                state.version,
                *self.store._scope(context),
                job_id,
            ),
        )
        return job_id

    def _owned(self, connection: Database, lease: Lease) -> dict[str, Any]:
        self.store._head(connection, lease.context)

        # Run, job and expiry reads have known scoped inputs. Submit all three
        # in order while retaining their distinct statement snapshots and locks.
        # The expiry read executes after the job lock is acquired: if a renewal
        # or cancellation blocks that lock, time spent waiting cannot extend the
        # lease. Results are checked before this method permits any mutation.
        with connection.pipeline():
            current_cursor = connection.execute(
                "SELECT revision FROM runs WHERE tenant_id=%s AND conversation_id=%s "
                "AND run_id=%s FOR UPDATE",
                self.store._key(lease.context),
            )
            job_cursor = connection.execute(
                "SELECT * FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
                "AND job_id=%s FOR UPDATE",
                (*self.store._key(lease.context), lease.job_id),
            )
            fence_cursor = connection.execute(
                "SELECT 1 FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND lease_owner=%s AND ownership_token=%s AND status='leased' "
                "AND lease_expiry>clock_timestamp()",
                self._identity(lease),
            )

        # Ownership needs the current run revision rather than historical text.
        # Revision 4 rejects a lease prepared at revision 3; a missing scoped job
        # cannot be replaced by a job from another run or tenant. The final result
        # also verifies the original lease timeline and database-time ownership.
        current = current_cursor.fetchone()

        if current is None:
            raise StateNotFoundError("run unavailable")

        if current["revision"] != lease.checkpoint.run.revision:
            raise StateConflictError("run advanced beyond lease checkpoint")

        row = job_cursor.fetchone()

        if row is None:
            raise StateConflictError("job unavailable in this run")

        self._timeline(lease)

        if fence_cursor.fetchone() is None:
            raise StateConflictError("expired or stale lease")

        return row

    def _fence(self, connection: Database, lease: Lease) -> None:
        self._timeline(lease)
        row = connection.execute(
            "SELECT 1 FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
            "AND lease_owner=%s AND ownership_token=%s AND status='leased' "
            "AND lease_expiry>clock_timestamp()",
            self._identity(lease),
        ).fetchone()

        if row is None:
            raise StateConflictError("expired or stale lease")

    def _recover_claim(self, connection: Database, row: dict[str, Any]) -> Lease | None:
        owner = connection.execute(
            "SELECT user_id FROM conversations WHERE tenant_id=%s AND conversation_id=%s",
            (row["tenant_id"], row["conversation_id"]),
        ).fetchone()
        assert owner is not None
        context = RunContext(
            row["tenant_id"], owner["user_id"], row["conversation_id"], row["run_id"]
        )
        head = self.store._head(connection, context)
        checkpoint = self.store._load(connection, context, head)
        lease = Lease(
            context,
            row["job_id"],
            row["lease_owner"],
            row["ownership_token"],
            checkpoint,
            Recovery.decode(row["recovery"]),
            row["attempt"],
            row["effect_pending"],
        )
        self._fence(connection, lease)
        return lease

    def _row(self, connection: Database, context: RunContext, job_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM jobs WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s "
            "AND job_id=%s FOR UPDATE",
            (*self.store._key(context), job_id),
        ).fetchone()

        if row is None:
            raise StateConflictError("job unavailable in this run")

        return row

    def _park(
        self, connection: Database, context: RunContext, job_id: str, state: Recovery, status: str
    ) -> None:
        phase = "reconcile" if status == "reconcile" else state.phase
        connection.execute(
            "UPDATE jobs SET status=%s, recovery=%s, failure_reason='unknown_effect', "
            "due_at=clock_timestamp()+interval '1 second' "
            "WHERE tenant_id=%s AND conversation_id=%s "
            "AND job_id=%s",
            (
                status,
                Jsonb(state.model_copy(update={"phase": phase}).encode()),
                *self.store._scope(context),
                job_id,
            ),
        )

    def _stop(
        self, connection: Database, context: RunContext, job_id: str, reason: FailureReason
    ) -> None:
        connection.execute(
            "UPDATE jobs SET status='stopped', failure_reason=%s, lease_expiry=NULL "
            "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
            (reason.value, *self.store._scope(context), job_id),
        )

    def _approval_current(self, connection: Database, context: RunContext, job_id: str) -> bool:
        row = connection.execute(
            "SELECT (wait_expiry IS NULL OR (approval_fingerprint IS NOT NULL "
            "AND wait_expiry>clock_timestamp())) AS allowed FROM jobs WHERE tenant_id=%s "
            "AND conversation_id=%s AND job_id=%s",
            (*self.store._scope(context), job_id),
        ).fetchone()
        return row is not None and row["allowed"]

    def _timeline(self, lease: Lease) -> None:
        if lease.checkpoint.conversation.recovery_id != self.store.recovery_id:
            raise StateConflictError("lease belongs to another recovery timeline")

    def _finish_exhausted(
        self,
        connection: Database,
        context: RunContext,
        job_id: str,
        *,
        approval: bool = False,
        reason: FailureReason = FailureReason.ATTEMPTS_EXHAUSTED,
    ) -> None:

        # Exhaustion without an uncertain effect ends the run and activates its
        # next turn. A prepared operation with no known outcome stays stopped;
        # its operation ID remains available for reconciliation or cancellation.
        # Budget exhaustion never proves that the external effect did not happen.
        head = self.store._head(connection, context)
        checkpoint = self.store._load(connection, context, head)
        row = self._row(connection, context, job_id)

        if approval:
            reason = FailureReason.APPROVAL_EXPIRED
        elif row["crash_count"] >= Recovery.decode(row["recovery"]).attempt_limit:
            reason = FailureReason.CRASH_LIMIT

        if row["effect_pending"]:
            self._stop(connection, context, job_id, reason)
            return

        connection.execute(
            "UPDATE jobs SET status='completed' WHERE tenant_id=%s AND conversation_id=%s "
            "AND job_id=%s",
            (*self.store._scope(context), job_id),
        )
        status = (
            RunStatus.LIMIT_REACHED
            if checkpoint.run.status == RunStatus.RUNNING and not approval
            else RunStatus.FAILED
        )
        self.store.checkpoint_change(
            context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            status=status,
        )(connection)
        connection.execute(
            "UPDATE jobs SET status=%s, failure_reason=%s, lease_expiry=NULL "
            "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
            (
                "approval_expired" if approval else "exhausted",
                reason.value,
                *self.store._scope(context),
                job_id,
            ),
        )

    def _key(self, lease: Lease) -> tuple[str, str, str]:
        return (*self.store._scope(lease.context), lease.job_id)

    def _identity(self, lease: Lease) -> tuple[str, str, str, str, int]:
        return (*self._key(lease), lease.owner, lease.token)
