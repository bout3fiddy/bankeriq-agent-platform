"""Primary-only leases and atomic settlement over the existing journal/job records."""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import TERMINAL_STATUSES, RunStatus
from agent_runtime.storage.interface import Checkpoint, StateConflictError
from bankeriq_agent_platform.storage.codec import fingerprint, plain
from bankeriq_agent_platform.storage.postgres import Database, PostgresStateStore

from .state import Completion, Handler, Handlers, Recovery, replay_safe


@dataclass(frozen=True)
class Lease:
    context: RunContext
    job_id: str
    owner: str
    token: int
    checkpoint: Checkpoint
    state: Recovery
    attempt: int


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

        versions = [version for kind, version in handlers.entries if kind == queue]

        if not versions:
            return None

        owner = str(uuid4())

        def transaction(connection: Database) -> Lease | None:
            self.store._generation(connection)

            # A single short admission lock makes queue and tenant counts exact
            # across processes. It is never held during execution or settlement.
            # SKIP LOCKED below prevents one busy conversation blocking the scan.
            connection.execute("SELECT pg_advisory_xact_lock(720322)")
            limits = connection.execute(
                "SELECT * FROM worker_limits WHERE queue=%s", (queue,)
            ).fetchone()

            if limits is None:
                raise ValueError("queue limits must be configured before claiming")

            previous = connection.execute(
                "SELECT * FROM jobs WHERE lease_owner=%s", (owner,)
            ).fetchone()

            if previous:
                return self._recover_claim(connection, previous)

            count = connection.execute(
                "SELECT count(*) AS n FROM jobs WHERE operation=%s AND status='leased' "
                "AND lease_expiry>clock_timestamp()",
                (queue,),
            ).fetchone()
            assert count is not None

            if count["n"] >= limits["concurrency"]:
                return None

            candidates = connection.execute(
                "SELECT j.*, c.user_id FROM jobs j JOIN conversations c USING "
                "(tenant_id,conversation_id) JOIN runs r USING (tenant_id,conversation_id,run_id) "
                "WHERE j.operation=%s AND j.operation_version=ANY(%s) "
                "AND j.recovery IS NOT NULL AND NOT c.deleted "
                "AND (SELECT count(*) FROM jobs busy WHERE busy.tenant_id=j.tenant_id "
                "AND busy.operation=j.operation AND busy.status='leased' "
                "AND busy.lease_expiry>clock_timestamp()) < %s "
                "AND r.active AND c.active_run_id=j.run_id "
                "AND ((j.status IN ('ready','retry','reconcile', "
                "'approval') AND j.due_at<=clock_timestamp()) OR "
                "(j.status='leased' AND j.lease_expiry<=clock_timestamp())) "
                "ORDER BY j.due_at,j.job_id LIMIT 64 FOR UPDATE OF c SKIP LOCKED",
                (queue, versions, limits["tenant_concurrency"]),
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
                    "SELECT ((status IN ('ready','retry','approval','reconcile') "
                    "AND due_at<=clock_timestamp()) "
                    "OR (status='leased' AND lease_expiry<=clock_timestamp())) AS ok FROM jobs "
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
                    continue

                if state.phase == "dispatch" and not replay_safe(state, handler):
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
                    if state.phase != "reconcile":
                        self._finish_exhausted(connection, context, row["job_id"])

                    continue

                connection.execute(
                    "UPDATE jobs SET status='leased', attempt=attempt+1, lease_owner=%s, "
                    "lease_expiry=clock_timestamp()+%s*interval '1 second', "
                    "ownership_token=ownership_token+1 WHERE tenant_id=%s "
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
                )

            return None

        return self.store._retry(transaction)

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

            prepared = state.model_copy(update={"phase": "dispatch"})
            connection.execute(
                "UPDATE jobs SET recovery=%s WHERE tenant_id=%s AND conversation_id=%s "
                "AND job_id=%s",
                (Jsonb(prepared.encode()), *self._key(lease)),
            )
            return prepared

        return self.store._retry(transaction)

    def settle(self, lease: Lease, completion: Completion) -> Checkpoint:
        """Fence ownership, then reuse the journal's atomic checkpoint transition."""

        payload = plain(completion)
        payload["next_task"] = completion.next_task.encode() if completion.next_task else None
        digest = fingerprint(payload)
        change = self.store.checkpoint_change(
            lease.context,
            expected_conversation_revision=lease.checkpoint.conversation.revision,
            expected_run_revision=lease.checkpoint.run.revision,
            expected_recovery_id=lease.checkpoint.conversation.recovery_id,
            expected_deletion_generation=lease.checkpoint.conversation.deletion_generation,
            status=completion.status,
            items=completion.items,
            pending_calls=completion.pending,
            artifacts=completion.artifacts,
        )

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

            if completion.next_task:
                self._attach(connection, checkpoint, completion.next_task)

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
                "recovery=jsonb_set(recovery,'{phase}','\"complete\"') "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s "
                "AND lease_owner=%s AND ownership_token=%s AND lease_expiry>clock_timestamp()",
                (digest, Jsonb(response["response"]), *self._identity(lease)),
            ).rowcount

            if updated != 1:
                raise StateConflictError("lease expired during settlement")

            self.store._fault("worker_after_settlement")
            return checkpoint

        return self.store._retry(transaction)

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

            phase = "approval" if approval else "retry"
            connection.execute(
                "UPDATE jobs SET status=%s, recovery=%s, control_fingerprint=%s, "
                "due_at=clock_timestamp()+%s*interval '1 second', "
                "wait_expiry=CASE WHEN %s THEN clock_timestamp()+%s*interval '1 second' END "
                "WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
                (
                    phase,
                    Jsonb(state.model_copy(update={"phase": phase}).encode()),
                    digest,
                    seconds,
                    approval,
                    seconds,
                    *self._key(lease),
                ),
            )

        self.store._retry(transaction)

    def reconcile(self, lease: Lease) -> None:
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
                "UPDATE jobs SET status=CASE WHEN recovery->>'phase' IN ('dispatch','reconcile') "
                "THEN 'cancelled_unknown' ELSE 'cancelled' END "
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
        head = self.store._head(connection, lease.context)
        checkpoint = self.store._load(connection, lease.context, head)

        if checkpoint.run.revision != lease.checkpoint.run.revision:
            raise StateConflictError("run advanced beyond lease checkpoint")

        row = self._row(connection, lease.context, lease.job_id)
        self._fence(connection, lease)
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
            "UPDATE jobs SET status=%s, recovery=%s, due_at=clock_timestamp()+interval '1 second' "
            "WHERE tenant_id=%s AND conversation_id=%s "
            "AND job_id=%s",
            (
                status,
                Jsonb(state.model_copy(update={"phase": phase}).encode()),
                *self.store._scope(context),
                job_id,
            ),
        )

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
    ) -> None:

        # Attempt/run budgets and approval expiry are durable terminal reasons.
        # For example, the third failed attempt at revision 1 ends the run and
        # releases the next accepted turn; it cannot leave the active slot stuck.
        head = self.store._head(connection, context)
        checkpoint = self.store._load(connection, context, head)
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
            "UPDATE jobs SET status=%s WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
            ("approval_expired" if approval else "exhausted", *self.store._scope(context), job_id),
        )

    def _key(self, lease: Lease) -> tuple[str, str, str]:
        return (*self.store._scope(lease.context), lease.job_id)

    def _identity(self, lease: Lease) -> tuple[str, str, str, str, int]:
        return (*self._key(lease), lease.owner, lease.token)
