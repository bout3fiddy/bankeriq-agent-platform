"""Bounded process-local scheduling with database-authoritative cross-process limits."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Thread
from time import monotonic
from typing import Any

import psycopg

from agent_harness.storage.interface import StateConflictError
from agent_runtime.storage.postgres import CommitUnknownError
from agent_runtime.workers.queue import ApprovalExpiredError, Lease, PostgresQueue
from agent_runtime.workers.reasons import FailureReason
from agent_runtime.workers.state import Handlers, policy_permitted, replay_safe


class Listener:
    """One dedicated autocommit session; payloads are wake hints and never work inputs."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., psycopg.Connection[Any]] = psycopg.connect,
    ) -> None:
        self.dsn = dsn
        self.connect = connect
        self.connection: psycopg.Connection[Any] | None = None

    def wait(self, seconds: float) -> None:
        try:
            if self.connection is None:
                self.connection = self.connect(
                    self.dsn, autocommit=True, target_session_attrs="read-write"
                )
                self.connection.execute("LISTEN runtime_jobs")

            # Even an invalid or duplicate ID only shortens this bounded wait.
            # The next primary scan applies scope, due time, version and ownership.
            for _ in self.connection.notifies(timeout=seconds, stop_after=1):
                break
        except psycopg.Error:
            self.close()

            # A dead listener cannot turn the poll loop into a connection storm.
            Event().wait(seconds)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class Worker:
    """Stop claiming before drain; retain heartbeats until in-flight work finishes.

    Example: Worker(queue, handlers, queue_name="synthetic", concurrency=2).run(stop).
    A signal handler sets stop. At most two handlers execute in this process;
    database limits additionally bound all processes for the queue and each tenant.
    Maintenance can use a separate single-slot Worker in the same deployment.
    """

    def __init__(
        self,
        queue: PostgresQueue,
        handlers: Handlers,
        *,
        queue_name: str,
        concurrency: int = 2,
        lease_seconds: float = 30,
        poll_seconds: float = 1,
        listener: Listener | None = None,
        maintenance: Callable[[], None] | None = None,
        connection_budget: int = 40,
        api_reserve: int = 8,
        process_limit: int = 2,
    ) -> None:
        if concurrency < 1 or not 0 < poll_seconds <= 10 or not 0 < lease_seconds <= 3600:
            raise ValueError("invalid worker bounds")

        # Each slot can have one DB write plus one heartbeat connection. Claim
        # and LISTEN require two more per process. Reject a configured topology
        # that would consume the API reserve; deployment must cap process count.
        if (
            process_limit < 1
            or api_reserve < 1
            or process_limit * (2 * concurrency + 2) > connection_budget - api_reserve
        ):
            raise ValueError("worker topology exceeds reserved connection capacity")

        self.maintenance = maintenance
        self.queue = queue
        self.handlers = handlers
        self.queue_name = queue_name
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.listener = listener

    def run(self, stop: Event) -> None:
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            pending: set[Future[None]] = set()
            maintenance_future: Future[None] | None = None
            next_maintenance = 0.0

            try:
                while not stop.is_set():
                    finished = {future for future in pending if future.done()}

                    for future in finished:
                        future.result()

                    pending -= finished

                    if (
                        self.maintenance is not None
                        and len(pending) < self.concurrency
                        and monotonic() >= next_maintenance
                        and (maintenance_future is None or maintenance_future.done())
                    ):
                        maintenance_future = executor.submit(self.maintenance)
                        pending.add(maintenance_future)
                        next_maintenance = monotonic() + self.poll_seconds

                    if len(pending) < self.concurrency and not stop.is_set():
                        try:
                            lease = self.queue.claim(
                                self.queue_name, self.handlers, seconds=self.lease_seconds
                            )
                        except psycopg.OperationalError, CommitUnknownError:

                            # Keep polling through primary outages. An uncertain
                            # claim retains its durable owner and expires normally.
                            lease = None

                        if lease is not None:

                            # No provider was contacted by this claim. Return its
                            # reservation without recording a crash or failure.
                            if stop.is_set():
                                self.queue.release(lease)
                                break

                            pending.add(executor.submit(self.execute, lease))
                            continue

                    if self.listener is not None:
                        self.listener.wait(self.poll_seconds)
                    else:
                        stop.wait(self.poll_seconds)
            finally:
                if self.listener is not None:
                    self.listener.close()

                # Executor shutdown waits for active calls with their renewal
                # threads still running. Hosting supplies the final hard deadline;
                # forced termination then follows the same lease recovery path.
                for future in pending:
                    future.result()

    def execute(self, lease: Lease) -> None:
        done = Event()
        lost = Event()

        def heartbeat() -> None:
            while not done.wait(self.lease_seconds / 3):
                try:
                    self.queue.renew(lease, seconds=self.lease_seconds)
                except psycopg.Error, StateConflictError, CommitUnknownError:
                    lost.set()
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()

        try:
            handler = self.handlers.resolve(lease.state)

            # Recheck access with fresh dependencies after every wait, retry and
            # restart. A revoked permission stops execution with a fixed reason;
            # an unknown effect retains its identity for later reconciliation.
            if not policy_permitted(handler.authorize, lease.context, lease.state):
                self.queue.reject(lease, FailureReason.ACCESS_DENIED)
                return

            if not policy_permitted(handler.approval_valid, lease.context, lease.state):
                self.queue.reject(lease, FailureReason.APPROVAL_DENIED)
                return

            if not self.queue.approval_current(lease):
                self.queue.reject(lease, FailureReason.APPROVAL_EXPIRED)
                return

            if lease.state.phase == "reconcile":
                assert handler.reconcile is not None
                self.queue.begin_lookup(lease)

                try:
                    outcome = handler.reconcile(lease.context, lease.state)
                except Exception:
                    outcome = None

                if outcome is None:
                    self.queue.reconcile(lease)
                    return
            else:
                if lease.effect_pending and not replay_safe(lease.state, handler):
                    self.queue.reconcile(lease)
                    return

                prepared = self.queue.prepare(lease)

                if lost.is_set():
                    return

                try:
                    outcome = handler.execute(lease.context, prepared)
                except Exception:

                    # An exception gives no evidence that the external effect
                    # failed. Persist retry only when both policy snapshots permit
                    # replay or the registered operation supports idempotency.
                    if replay_safe(prepared, handler):
                        self.queue.pause(
                            lease, seconds=min(300, 2 ** (lease.attempt - 1)), handler=handler
                        )
                    else:
                        self.queue.reconcile(lease)

                    return

            if not lost.is_set():

                # settle retries only this captured result's database writes.
                # Its retry loop has no reference to handler.execute.
                self.queue.settle(lease, outcome)
        except ApprovalExpiredError:
            self.queue.reject(lease, FailureReason.APPROVAL_EXPIRED)
        except StateConflictError, CommitUnknownError, psycopg.OperationalError:

            # A late or uncertain result cannot authorize another dispatch here.
            # The durable dispatch phase forces replay policy or reconciliation
            # when a subsequent worker recovers the lease.
            return
        finally:
            done.set()
            thread.join()
