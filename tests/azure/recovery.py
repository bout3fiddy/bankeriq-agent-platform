"""Fence a disposable restored database before opening API admission or dispatch."""

from uuid import UUID

from agent_runtime.storage.interface import StateConflictError
from bankeriq_agent_platform.storage.connections import PrimaryConnections
from bankeriq_agent_platform.storage.postgres import PostgresStateStore


def prepare_restore(connections: PrimaryConnections, recovery_id: str) -> bool:
    """Advance the restored timeline once, with externally paused workers.

    The operator supplies a new UUID after stopping all three apps and reconciling
    acknowledged requests against the restored point. For example, generation A
    becomes B and a lease obtained on A loses renewal authority. Retrying B is a
    no-op. This synthetic rehearsal rejects unresolved effects; it cannot replace
    a production provider receipt ledger or authorise replay of unknown effects.
    """

    recovery_id = str(UUID(recovery_id))
    stale = PostgresStateStore(connections.dsn, connections=connections)

    with connections.connection() as connection:
        current = connection.execute(
            "SELECT recovery_id FROM runtime_generation FOR UPDATE"
        ).fetchone()
        assert current is not None

        if current["recovery_id"] == recovery_id:
            return False

        pending = connection.execute(
            "SELECT count(*) AS n FROM jobs WHERE effect_pending"
        ).fetchone()
        assert pending is not None

        if pending["n"]:
            raise StateConflictError("reconcile unknown effects before restoring dispatch")

        # Commit the new timeline and lease expiry together. New processes pin B;
        # an old A process cannot settle or renew through its cached store object.
        connection.execute("UPDATE runtime_generation SET recovery_id=%s", (recovery_id,))
        connection.execute(
            "UPDATE jobs SET lease_expiry=clock_timestamp()-interval '1 second' "
            "WHERE status='leased'"
        )

    try:
        with connections.connection() as connection:
            stale._generation(connection)
    except StateConflictError:
        return True

    raise AssertionError("old database generation retained authority")
