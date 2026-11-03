"""Primary-side queue observations for externally owned scaling decisions."""

from dataclasses import dataclass

from .queue import PostgresQueue
from .state import Handlers


@dataclass(frozen=True)
class QueuePressure:
    eligible: int
    oldest_seconds: float
    leased: int
    concurrency: int
    capped: bool


def pressure(queue: PostgresQueue, kind: str, handlers: Handlers) -> QueuePressure:
    """Count due compatible execution work, excluding approval and future retry waits.

    This is an observation, not dispatch authority. For example, 50 sleeping retries
    and 3 ready jobs report eligible=3; each actual claim still checks live permissions,
    run budget, resource ownership and tenant capacity. Bound the observation at 10,000
    rows and report saturation so an operator cannot mistake a capped count for exact.
    """

    versions = [version for name, version in handlers.entries if name == kind]
    lookup_versions = [
        version
        for (name, version), handler in handlers.entries.items()
        if name == kind and handler.reconcile is not None
    ]

    idempotent = [v for (name, v), h in handlers.entries.items() if name == kind and h.idempotent]
    replay = [v for (name, v), h in handlers.entries.items() if name == kind and h.replay_allowed]

    with queue.store._connect() as connection:
        queue.store._generation(connection)
        limits = connection.execute(
            "SELECT concurrency,tenant_concurrency FROM worker_limits WHERE queue=%s", (kind,)
        ).fetchone()

        if limits is None:
            raise ValueError("queue must be configured before observing capacity")

        row = connection.execute(
            "SELECT count(*) AS n,COALESCE(max(age),0) AS age FROM (SELECT "
            "extract(epoch FROM (clock_timestamp()-j.due_at)) AS age FROM jobs j "
            "JOIN conversations c USING (tenant_id,conversation_id) "
            "JOIN runs r USING (tenant_id,conversation_id,run_id) "
            "WHERE j.operation=%s AND j.operation_version=ANY(%s) AND j.recovery IS NOT NULL "
            "AND j.status IN ('ready','retry','reconcile') AND j.due_at<=clock_timestamp() "
            "AND (j.status<>'reconcile' OR j.operation_version=ANY(%s)) "
            "AND (NOT j.effect_pending OR j.status='reconcile' OR "
            "((j.recovery->>'idempotent')::boolean AND j.operation_version=ANY(%s)) OR "
            "((j.recovery->>'replay_allowed')::boolean AND j.operation_version=ANY(%s))) "
            "AND (SELECT count(*) FROM jobs active WHERE active.tenant_id=j.tenant_id "
            "AND active.operation=j.operation AND active.status='leased' "
            "AND active.lease_expiry>clock_timestamp())<%s "
            "AND r.active AND c.active_run_id=j.run_id AND NOT c.deleted "
            "AND r.worker_attempts<r.worker_budget "
            "AND (j.attempt<(j.recovery->>'attempt_limit')::integer OR j.status='reconcile') "
            "AND (j.resource_key IS NULL OR NOT EXISTS (SELECT 1 FROM jobs busy "
            "WHERE busy.tenant_id=j.tenant_id AND busy.resource_key=j.resource_key "
            "AND busy.job_id<>j.job_id AND (busy.effect_pending OR "
            "(busy.status='leased' AND busy.lease_expiry>clock_timestamp())))) "
            "ORDER BY j.due_at,j.job_id LIMIT 10000) due",
            (kind, versions, lookup_versions, idempotent, replay, limits["tenant_concurrency"]),
        ).fetchone()
        leased = connection.execute(
            "SELECT count(*) AS n FROM jobs WHERE operation=%s AND status='leased' "
            "AND lease_expiry>clock_timestamp()",
            (kind,),
        ).fetchone()
        assert row is not None and leased is not None
        return QueuePressure(
            row["n"], float(row["age"]), leased["n"], limits["concurrency"], row["n"] == 10000
        )
