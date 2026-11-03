"""Authorised inspection and audited recovery commands at the application boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import TERMINAL_STATUSES, RunStatus
from agent_runtime.storage.interface import StateConflictError
from bankeriq_agent_platform.storage.codec import fingerprint, plain
from bankeriq_agent_platform.storage.postgres import Database

from .queue import PostgresQueue
from .reasons import FailureReason
from .state import Handler, Handlers, Recovery, permitted, policy_permitted, replay_safe

type Action = Literal["inspect", "retry", "reconcile"]
type Decision = Literal[
    "accepted",
    "access_denied",
    "approval_denied",
    "handler_unavailable",
    "ineligible",
    "stale",
    "replay_denied",
]


@dataclass(frozen=True)
class RecoveryActor:
    """Verified application identity; request bodies must never choose this actor."""

    tenant_id: str
    actor_id: str


class ControlRequest(BaseModel):
    """An exact inspected revision and timeline, with a stable caller request ID."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(min_length=1, max_length=200)
    action: Literal["retry", "reconcile"]
    expected_revision: int = Field(ge=0)
    recovery_id: str = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class ControlReceipt:
    decision: Decision
    revision: int


@dataclass(frozen=True)
class JobInspection:
    """Allowlisted fields: no arguments, provider exceptions, output or credentials."""

    job_id: str
    operation_id: str
    status: str
    reason: FailureReason
    revision: int
    recovery_id: str
    attempts: int
    attempt_limit: int
    crashes: int
    run_attempts: int
    run_budget: int
    unknown_effect: bool
    allowed_actions: tuple[str, ...]
    parent_run_id: str = ""
    group_id: str | None = None
    call_id: str | None = None


class RecoveryControls:
    """Trusted composition supplies identity, current policy and compatible handlers.

    Example: authorize(actor, context, action) checks actor.tenant_id and a live
    entitlement for action="retry". Pass it as authorize=authorize, then inspect
    job-7 and apply ControlRequest(request_id="retry-1", expected_revision=4, ...).
    Revision 4 -> 5 and the audit decision commit together. A retry of retry-1
    returns that original receipt; different content under retry-1 conflicts.

    Policy/provider callbacks run outside database retry transactions. Every new
    command revalidates the observed revision under locks; workers check current
    execution access and approvals again before dispatch. Restored or exhausted
    work cannot acquire a fresh budget through these controls.
    """

    def __init__(
        self,
        queue: PostgresQueue,
        handlers: Handlers,
        *,
        authorize: Callable[[RecoveryActor, RunContext, Action], bool],
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.authorize = authorize

    def inspect(self, actor: RecoveryActor, context: RunContext, job_id: str) -> JobInspection:
        self._scope(actor, context)

        if not permitted(lambda: self.authorize(actor, context, "inspect")):
            raise PermissionError("worker inspection denied")

        with self.queue.store._connect() as connection:
            row = self._snapshot(connection, context, job_id)

        state = Recovery.decode(row["recovery"])
        handler = self.handlers.entries.get((state.kind, state.version))
        access, approval = self._execution_access(context, state, handler)
        reason = FailureReason(row["failure_reason"])

        # Compatibility is local to the inspecting deployment. A v1-only worker
        # must not globally stop a v2 job which another registered worker can run.
        # Persisted crash/budget reasons retain priority over this derived view.
        if handler is None and reason in (
            FailureReason.NONE,
            FailureReason.PROCESS_LOST,
            FailureReason.EXECUTION_RETRY,
        ):
            reason = FailureReason.HANDLER_UNAVAILABLE

        actions = tuple(
            action
            for action in ("retry", "reconcile")
            if permitted(lambda action=action: self.authorize(actor, context, action))
            and self._eligibility(row, state, handler, action, access, approval) == "accepted"
        )
        return JobInspection(
            job_id,
            state.operation_id,
            row["status"],
            reason,
            row["control_revision"],
            self.queue.store.recovery_id,
            row["attempt"],
            state.attempt_limit,
            row["crash_count"],
            row["run_attempts"],
            row["run_budget"],
            row["effect_pending"],
            actions,
            context.run_id,
            row["group_id"],
            row["call_id"],
        )

    def apply(
        self,
        actor: RecoveryActor,
        context: RunContext,
        job_id: str,
        request: ControlRequest,
    ) -> ControlReceipt:
        self._scope(actor, context)
        request = ControlRequest.model_validate(request.model_dump())
        authorized = permitted(lambda: self.authorize(actor, context, request.action))

        with self.queue.store._connect() as connection:
            observed = self._snapshot(connection, context, job_id)

        state = Recovery.decode(observed["recovery"])
        handler = self.handlers.entries.get((state.kind, state.version))
        access, approval = (
            self._execution_access(context, state, handler) if authorized else (False, False)
        )
        digest = fingerprint([plain(actor), plain(context), job_id, request.model_dump()])

        def transaction(connection: Database) -> ControlReceipt:
            row = self._snapshot(connection, context, job_id)
            prior = connection.execute(
                "SELECT fingerprint,response FROM worker_controls WHERE tenant_id=%s "
                "AND request_id=%s",
                (context.tenant_id, request.request_id),
            ).fetchone()

            if prior:
                if prior["fingerprint"] != digest:
                    raise StateConflictError("control request ID reused with conflicting content")

                # A duplicate is a receipt lookup, never a second recovery action.
                # Revoked control access still prevents returning that receipt;
                # the original immutable decision remains available to auditors.
                if not authorized:
                    return ControlReceipt("access_denied", 0)

                return ControlReceipt(**prior["response"])

            if not authorized:
                receipt = ControlReceipt("access_denied", 0)
            elif (
                request.recovery_id != self.queue.store.recovery_id
                or request.expected_revision != row["control_revision"]
                or observed["control_revision"] != row["control_revision"]
            ):
                receipt = ControlReceipt("stale", row["control_revision"])
            else:
                decision = self._eligibility(row, state, handler, request.action, access, approval)

                if decision == "accepted":

                    # Preserve identity, arguments, budgets and any unknown effect.
                    # A reconcile command schedules lookup only. A retry of an
                    # unknown effect requires both saved and current replay policy.
                    phase = "reconcile" if request.action == "reconcile" else "retry"
                    connection.execute(
                        "UPDATE jobs SET status=%s, recovery=%s, due_at=clock_timestamp(), "
                        "ownership_token=ownership_token+1, lease_expiry=NULL, "
                        "failure_reason=%s WHERE tenant_id=%s AND conversation_id=%s AND job_id=%s",
                        (
                            phase,
                            Jsonb(state.model_copy(update={"phase": phase}).encode()),
                            "unknown_effect" if row["effect_pending"] else "none",
                            *self.queue.store._scope(context),
                            job_id,
                        ),
                    )
                    row = self.queue._row(connection, context, job_id)

                receipt = ControlReceipt(decision, row["control_revision"])

            connection.execute(
                "INSERT INTO worker_controls (tenant_id,request_id,actor_id,conversation_id, "
                "run_id, "
                "job_id,action,fingerprint,decision,response) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.tenant_id,
                    request.request_id,
                    actor.actor_id,
                    context.conversation_id,
                    context.run_id,
                    job_id,
                    request.action,
                    digest,
                    receipt.decision,
                    Jsonb(plain(receipt)),
                ),
            )
            self.queue.store._fault("after_control_write")
            return receipt

        return self.queue.store._retry(transaction)

    def decisions(
        self,
        actor: RecoveryActor,
        context: RunContext,
        job_id: str,
        *,
        limit: int = 20,
    ) -> tuple[dict[str, str], ...]:
        """Read a bounded audit page after checking current inspection access."""

        self._scope(actor, context)

        if not 1 <= limit <= 100:
            raise ValueError("audit limit must be between 1 and 100")

        if not permitted(lambda: self.authorize(actor, context, "inspect")):
            raise PermissionError("worker inspection denied")

        with self.queue.store._connect() as connection:
            self._snapshot(connection, context, job_id)
            rows = connection.execute(
                "SELECT request_id,actor_id,action,decision FROM worker_controls "
                "WHERE tenant_id=%s AND conversation_id=%s AND run_id=%s AND job_id=%s "
                "ORDER BY created_at DESC,request_id LIMIT %s",
                (*self.queue.store._key(context), job_id, limit),
            ).fetchall()
            return tuple(rows)

    def _snapshot(self, connection: Database, context: RunContext, job_id: str) -> dict[str, Any]:
        head = self.queue.store._head(connection, context)
        self.queue.store._load(connection, context, head)
        run = connection.execute(
            "SELECT worker_attempts,worker_budget,status,active FROM runs WHERE tenant_id=%s "
            "AND conversation_id=%s AND run_id=%s",
            self.queue.store._key(context),
        ).fetchone()
        assert run is not None
        row = self.queue._row(connection, context, job_id)
        return dict(
            row,
            run_attempts=run["worker_attempts"],
            run_budget=run["worker_budget"],
            run_status=run["status"],
            active=run["active"],
            approval_current=self.queue._approval_current(connection, context, job_id),
        )

    def _execution_access(
        self,
        context: RunContext,
        state: Recovery,
        handler: Handler | None,
    ) -> tuple[bool, bool]:
        if handler is None:
            return False, False

        return (
            policy_permitted(handler.authorize, context, state),
            policy_permitted(handler.approval_valid, context, state),
        )

    def _eligibility(
        self,
        row: dict[str, Any],
        state: Recovery,
        handler: Handler | None,
        action: str,
        access: bool,
        approval: bool,
    ) -> Decision:
        if (
            row["settlement_fingerprint"] is not None
            or RunStatus(row["run_status"]) in TERMINAL_STATUSES
            or not row["active"]
            or row["status"] not in ("ready", "retry", "reconcile", "stopped")
            or row["run_attempts"] >= row["run_budget"]
        ):
            return "ineligible"

        if handler is None:
            return "handler_unavailable"

        if not access:
            return "access_denied"

        if not approval or not row["approval_current"]:
            return "approval_denied"

        if action == "reconcile":
            return "accepted" if row["effect_pending"] and handler.reconcile else "ineligible"

        if row["attempt"] >= state.attempt_limit or state.phase == "reconcile":
            return "ineligible"

        if row["effect_pending"] and not replay_safe(state, handler):
            return "replay_denied"

        return "accepted"

    @staticmethod
    def _scope(actor: RecoveryActor, context: RunContext) -> None:
        if actor.tenant_id != context.tenant_id:
            raise PermissionError("worker control tenant mismatch")

        if not actor.actor_id or len(actor.actor_id) > 200:
            raise ValueError("invalid verified actor identity")
