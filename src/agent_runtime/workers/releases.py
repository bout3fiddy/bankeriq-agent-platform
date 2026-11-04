"""Audited release drain and atomic pre-acceptance backlog bounds."""

from collections.abc import Callable
from typing import Any

from agent_harness.runs.context import RunContext
from agent_harness.storage.interface import StateConflictError
from agent_runtime.hosting.access import Principal
from agent_runtime.storage.postgres import Database, PostgresStateStore


class AdmissionPaused(RuntimeError):
    """No new input was accepted while an operator drains the current deployment."""


class AdmissionOverloaded(RuntimeError):
    """No new input was accepted because a configured unfinished-run bound was reached."""


def check_admission(connection: Database, context: RunContext, request_id: str) -> None:
    """Serialize capacity allocation with the input transaction and preserve replay.

    A retry of accepted request A still resolves A while admission is closed.
    A new request B observes the gate and pending-run counts under one row lock,
    so two API replicas cannot both consume the final available reservation.
    Its insertion, job and reservation are all the same committed run record.
    """

    prior = connection.execute(
        "SELECT 1 FROM admissions WHERE tenant_id=%s AND request_id=%s",
        (context.tenant_id, request_id),
    ).fetchone()

    if prior is not None:
        return

    gate = connection.execute("SELECT * FROM runtime_admission FOR UPDATE").fetchone()
    assert gate is not None

    if not gate["open"]:
        raise AdmissionPaused("admission paused for release")

    if not gate["bounded"]:
        return

    counts = connection.execute(
        "SELECT count(*) AS global_count, count(*) FILTER (WHERE tenant_id=%s) AS tenant_count, "
        "count(*) FILTER (WHERE tenant_id=%s AND conversation_id=%s) AS conversation_count "
        "FROM runs WHERE status IN ('queued','running','waiting')",
        (context.tenant_id, *PostgresStateStore._scope(context)),
    ).fetchone()
    assert counts is not None

    if any(
        counts[name + "_count"] >= gate[name + "_limit"]
        for name in ("global", "tenant", "conversation")
    ):
        raise AdmissionOverloaded("unfinished run capacity reached")


class ReleaseControls:
    """Require explicit operator policy before inspecting or changing global admission.

    The application supplies authorize(principal); ordinary conversation ownership
    grants no release authority. Closing admission drains accepted jobs normally.
    It neither cancels effects nor changes worker budgets or retained checkpoints.
    """

    def __init__(
        self, store: PostgresStateStore, *, authorize: Callable[[Principal], bool]
    ) -> None:
        self.store, self.authorize = store, authorize

    def inspect(self, actor: Principal) -> dict[str, Any]:
        self._check(actor)

        with self.store._connect() as connection:
            self.store._generation(connection)
            gate = connection.execute("SELECT * FROM runtime_admission").fetchone()
            jobs = connection.execute(
                "SELECT operation,status,count(*) AS count FROM jobs WHERE recovery IS NOT NULL "
                "AND status NOT IN ('completed','failed','cancelled') GROUP BY operation,status"
            ).fetchall()
            active = connection.execute(
                "SELECT count(*) AS unfinished FROM runs "
                "WHERE status IN ('queued','running','waiting')"
            ).fetchone()

        return {"admission": gate, "jobs": jobs, **(active or {})}

    def change(
        self, actor: Principal, *, request_id: str, open: bool, expected_revision: int
    ) -> int:
        self._check(actor)

        if not 1 <= len(request_id) <= 200 or type(open) is not bool or expected_revision < 0:
            raise ValueError("invalid release request")

        def transaction(connection: Database) -> int:
            self.store._generation(connection)
            gate = connection.execute("SELECT * FROM runtime_admission FOR UPDATE").fetchone()
            assert gate is not None
            prior = connection.execute(
                "SELECT * FROM runtime_release_changes WHERE request_id=%s", (request_id,)
            ).fetchone()

            if prior is not None:
                if (
                    prior["actor_tenant"],
                    prior["actor_user"],
                    prior["requested_open"],
                    prior["expected_revision"],
                ) != (actor.tenant_id, actor.user_id, open, expected_revision):
                    raise StateConflictError("release request identity conflicts")

                return prior["result_revision"]

            if gate["revision"] != expected_revision:
                raise StateConflictError("release revision changed")

            revision = expected_revision + 1
            connection.execute("UPDATE runtime_admission SET open=%s,revision=%s", (open, revision))
            connection.execute(
                "INSERT INTO runtime_release_changes "
                "(request_id,actor_tenant,actor_user,requested_open,"
                "expected_revision,result_revision) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (request_id, actor.tenant_id, actor.user_id, open, expected_revision, revision),
            )
            return revision

        return self.store._retry(transaction)

    def _check(self, actor: Principal) -> None:
        try:
            allowed = self.authorize(actor)
        except Exception:
            allowed = False

        if not allowed:
            raise PermissionError("release control denied")
