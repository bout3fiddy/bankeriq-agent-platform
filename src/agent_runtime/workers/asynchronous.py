"""Bounded asyncio scheduling; blocking adapters occupy threads only during active work."""

import asyncio
from contextlib import suppress
from time import monotonic

import psycopg

from agent_harness.coordinator.model import ModelRateLimited
from agent_harness.storage.interface import StateConflictError
from agent_runtime.storage.postgres import CommitUnknownError
from agent_runtime.telemetry import ACTIVE_ATTEMPT, ACTIVE_JOB, ACTIVE_RUN, emit
from agent_runtime.workers.queue import ApprovalExpiredError, Lease
from agent_runtime.workers.reasons import FailureReason
from agent_runtime.workers.runner import Worker
from agent_runtime.workers.state import policy_permitted, replay_safe


class AsyncWorker(Worker):
    """Reuse leases and recovery policies with async model I/O and bounded tool isolation.

    run_async(stop) has at most concurrency execution tasks. Waiting parents have
    no lease or task, so ten thousand idle chats allocate no execution threads.
    Blocking tools run in the executor only while active; CPU-heavy handlers can
    supply an async_execute adapter backed by an application-owned process pool.
    """

    async def run_async(self, stop: asyncio.Event) -> None:
        pending: set[asyncio.Task[None]] = set()
        notification: asyncio.Task[None] | None = None

        try:
            while not stop.is_set():
                finished = {task for task in pending if task.done()}

                for task in finished:
                    task.result()

                pending -= finished

                if len(pending) < self.concurrency:
                    try:
                        lease = await asyncio.to_thread(
                            self.queue.claim,
                            self.queue_name,
                            self.handlers,
                            seconds=self.lease_seconds,
                        )
                    except psycopg.OperationalError, CommitUnknownError:

                        # A database outage admits no work. Keep the process alive
                        # and use the bounded poll delay before reconnecting to the
                        # primary; a lost claim remains recoverable by its lease.
                        lease = None

                    if lease is not None:
                        if stop.is_set():
                            await asyncio.to_thread(self.queue.release, lease)
                            break

                        pending.add(asyncio.create_task(self.execute_async(lease)))
                        continue

                if self.listener is not None:

                    # With both slots full, completion can free one before the
                    # notification wait returns. Wake on either event so job three
                    # can claim that slot immediately. Keep one listener task alive
                    # across iterations: cancelling to_thread() would leave its
                    # blocking wait running and could overlap another socket read.
                    if notification is None:
                        notification = asyncio.create_task(
                            asyncio.to_thread(self.listener.wait, self.poll_seconds)
                        )

                    await asyncio.wait(
                        pending | {notification}, return_when=asyncio.FIRST_COMPLETED
                    )

                    if notification.done():
                        notification.result()
                        notification = None
                elif pending:

                    # Poll-only workers also reuse a released slot without waiting
                    # for the next scan deadline. Idle chats still own no task.
                    await asyncio.wait(
                        pending,
                        timeout=self.poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.poll_seconds)
        finally:
            try:
                if pending:
                    await asyncio.gather(*pending)
            finally:

                # Drain the single blocking wait before closing its connection.
                # No listener thread survives an orderly worker shutdown, even if
                # an execution task failed while its notification was outstanding.
                try:
                    if notification is not None:
                        await notification
                finally:
                    if self.listener is not None:
                        await asyncio.to_thread(self.listener.close)

    async def execute_async(self, lease: Lease) -> None:
        lost = asyncio.Event()
        started = monotonic()
        context_token = ACTIVE_RUN.set(lease.context)
        job_token = ACTIVE_JOB.set(lease.job_id)
        attempt_token = ACTIVE_ATTEMPT.set(lease.attempt)
        emit(
            "worker.claimed",
            lease.context,
            queue=self.queue_name,
            attempt=lease.attempt,
            queue_wait_ms=lease.queue_wait_ms,
            queue_wait_upper_ms=lease.queue_wait_upper_ms,
        )

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.lease_seconds / 3)

                try:
                    await asyncio.to_thread(self.queue.renew, lease, seconds=self.lease_seconds)
                except psycopg.Error, StateConflictError, CommitUnknownError:
                    lost.set()
                    emit("worker.lease_lost", lease.context, queue=self.queue_name)
                    return

        renewal = asyncio.create_task(heartbeat())

        try:
            handler = self.handlers.resolve(lease.state)

            for policy, reason in (
                (handler.authorize, FailureReason.ACCESS_DENIED),
                (handler.approval_valid, FailureReason.APPROVAL_DENIED),
            ):
                if not await asyncio.to_thread(
                    policy_permitted, policy, lease.context, lease.state
                ):
                    await asyncio.to_thread(self.queue.reject, lease, reason)
                    return

            if not await asyncio.to_thread(self.queue.approval_current, lease):
                await asyncio.to_thread(self.queue.reject, lease, FailureReason.APPROVAL_EXPIRED)
                return

            if await asyncio.to_thread(
                handler.approval_required, lease.context, lease.state
            ) and not await asyncio.to_thread(self.queue.approval_granted, lease):
                await asyncio.to_thread(self.queue.pause, lease, seconds=300, approval=True)
                return

            if lease.state.phase == "reconcile":
                assert handler.reconcile is not None
                await asyncio.to_thread(self.queue.begin_lookup, lease)

                try:
                    outcome = await asyncio.to_thread(handler.reconcile, lease.context, lease.state)
                except Exception:
                    outcome = None

                if outcome is None:
                    await asyncio.to_thread(self.queue.reconcile, lease)
                    return
            else:
                if lease.effect_pending and not replay_safe(lease.state, handler):
                    await asyncio.to_thread(self.queue.reconcile, lease)
                    return

                prepared = await asyncio.to_thread(self.queue.prepare, lease)

                if lost.is_set():
                    return

                emit("worker.started", lease.context, queue=self.queue_name, attempt=lease.attempt)

                try:
                    if handler.async_execute_with_lease is not None:
                        outcome = await handler.async_execute_with_lease(
                            lease.context, prepared, lease
                        )
                    elif handler.async_execute is not None:
                        outcome = await handler.async_execute(lease.context, prepared)
                    else:
                        outcome = await asyncio.to_thread(handler.execute, lease.context, prepared)
                except ModelRateLimited as error:
                    if replay_safe(prepared, handler):
                        emit("worker.retry", lease.context, queue=self.queue_name)
                        await asyncio.to_thread(
                            self.queue.pause,
                            lease,
                            seconds=error.retry_after,
                            handler=handler,
                        )
                    else:
                        await asyncio.to_thread(self.queue.reconcile, lease)

                    return
                except Exception:
                    if replay_safe(prepared, handler):
                        emit("worker.retry", lease.context, queue=self.queue_name)
                        await asyncio.to_thread(
                            self.queue.pause,
                            lease,
                            seconds=min(300, 2 ** (lease.attempt - 1)),
                            handler=handler,
                        )
                    else:
                        emit("worker.uncertain", lease.context, queue=self.queue_name)
                        await asyncio.to_thread(self.queue.reconcile, lease)

                    return

            if not lost.is_set():
                settlement_started = monotonic()
                await asyncio.to_thread(self.queue.settle, lease, outcome)
                emit(
                    "runtime.span",
                    lease.context,
                    stage="settlement",
                    elapsed_ms=round((monotonic() - settlement_started) * 1000),
                )
                emit(
                    "worker.settled",
                    lease.context,
                    queue=self.queue_name,
                    attempt=lease.attempt,
                    elapsed_ms=round((monotonic() - started) * 1000),
                )
        except ApprovalExpiredError:
            await asyncio.to_thread(self.queue.reject, lease, FailureReason.APPROVAL_EXPIRED)
        except StateConflictError, CommitUnknownError, psycopg.OperationalError:
            return
        finally:
            ACTIVE_RUN.reset(context_token)
            ACTIVE_JOB.reset(job_token)
            ACTIVE_ATTEMPT.reset(attempt_token)
            renewal.cancel()

            with suppress(asyncio.CancelledError):
                await renewal
