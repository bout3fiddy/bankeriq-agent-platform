"""Bounded asyncio scheduling; blocking adapters occupy threads only during active work."""

import asyncio
from contextlib import suppress

import psycopg

from agent_runtime.storage.interface import StateConflictError
from bankeriq_agent_platform.storage.postgres import CommitUnknownError

from .queue import ApprovalExpiredError, Lease
from .reasons import FailureReason
from .runner import Worker
from .state import policy_permitted, replay_safe


class AsyncWorker(Worker):
    """Reuse leases and recovery policies with async model I/O and bounded tool isolation.

    run_async(stop) has at most concurrency execution tasks. Waiting parents have
    no lease or task, so ten thousand idle chats allocate no execution threads.
    Blocking tools run in the executor only while active; CPU-heavy handlers can
    supply an async_execute adapter backed by an application-owned process pool.
    """

    async def run_async(self, stop: asyncio.Event) -> None:
        pending: set[asyncio.Task[None]] = set()

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

                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll_seconds)
        finally:
            if pending:
                await asyncio.gather(*pending)

    async def execute_async(self, lease: Lease) -> None:
        lost = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.lease_seconds / 3)

                try:
                    await asyncio.to_thread(self.queue.renew, lease, seconds=self.lease_seconds)
                except psycopg.Error, StateConflictError, CommitUnknownError:
                    lost.set()
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

                try:
                    outcome = (
                        await handler.async_execute(lease.context, prepared)
                        if handler.async_execute is not None
                        else await asyncio.to_thread(handler.execute, lease.context, prepared)
                    )
                except Exception:
                    if replay_safe(prepared, handler):
                        await asyncio.to_thread(
                            self.queue.pause,
                            lease,
                            seconds=min(300, 2 ** (lease.attempt - 1)),
                            handler=handler,
                        )
                    else:
                        await asyncio.to_thread(self.queue.reconcile, lease)

                    return

            if not lost.is_set():
                await asyncio.to_thread(self.queue.settle, lease, outcome)
        except ApprovalExpiredError:
            await asyncio.to_thread(self.queue.reject, lease, FailureReason.APPROVAL_EXPIRED)
        except StateConflictError, CommitUnknownError, psycopg.OperationalError:
            return
        finally:
            renewal.cancel()

            with suppress(asyncio.CancelledError):
                await renewal
