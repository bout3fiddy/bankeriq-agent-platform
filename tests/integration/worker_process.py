"""Fresh-process synthetic handlers used by durable worker failure tests."""

import json
import os
import sys
import time
from pathlib import Path

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunStatus
from agent_harness.storage.codec import plain
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.queue import Lease, PostgresQueue
from agent_runtime.workers.runner import Worker
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery


def complete(context: RunContext, state: Recovery) -> Completion:
    if state.arguments.get("finish"):
        return Completion(RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, "finished"),))

    return Completion(
        RunStatus.RUNNING,
        (Message(MessageRole.ASSISTANT, "synthetic saved result"),),
        next_task=Recovery(
            kind=state.kind,
            operation_id=state.operation_id + "-next",
            arguments={"finish": True},
            idempotent=True,
            run_budget=state.run_budget,
        ),
    )


def main() -> None:
    mode = sys.argv[1]

    if mode == "codec":
        print(json.dumps(Recovery.decode(json.loads(sys.stdin.read())).encode()))
        return

    queue = PostgresQueue(PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"]))
    kind = sys.argv[2]
    handlers = Handlers({(kind, 1): Handler(lambda c, s: True, complete, idempotent=True)})

    if mode == "reconcile-run":

        def forbidden(context: RunContext, state: Recovery) -> Completion:
            raise AssertionError("unsafe operation must never execute again")

        def lookup(context: RunContext, state: Recovery) -> Completion:
            assert Path(sys.argv[3]).read_text() == state.operation_id
            return complete(context, state)

        handlers = Handlers({(kind, 1): Handler(lambda c, s: True, forbidden, reconcile=lookup)})

    if mode == "restore":
        data = json.loads(sys.stdin.read())
        context = RunContext(**data["context"])
        queue.store.load_run(context)

        with queue.store._connect() as connection:
            row = queue._row(connection, context, data["job_id"])
            print(json.dumps(Recovery.decode(row["recovery"]).encode()))

        return

    if mode == "claim":
        lease = queue.claim(kind, handlers, seconds=20)
        print(json.dumps(plain(lease.context) if lease else None))
        return

    if mode == "settle":
        data = json.loads(sys.stdin.read())
        context = RunContext(**data["context"])
        lease = Lease(
            context,
            data["job_id"],
            data["owner"],
            data["token"],
            queue.store.load_run(context),
            Recovery.decode(data["state"]),
            data["attempt"],
        )
        Path(sys.argv[3]).write_text("ready")

        while not Path(sys.argv[4]).exists():
            time.sleep(0.01)

        result = queue.settle(lease, complete(context, lease.state))
        print(result.run.revision)
        return

    lease = None
    deadline = time.monotonic() + 5

    while lease is None and time.monotonic() < deadline:
        lease = queue.claim(
            kind, handlers, seconds=0.5 if mode in ("die", "effect-die", "pre-die") else 5
        )

        if lease is None:
            time.sleep(0.03)

    assert lease is not None

    if mode in ("die", "effect-die", "pre-die"):
        if mode != "pre-die":
            queue.prepare(lease)

        Path(sys.argv[3]).write_text(lease.state.operation_id)
        time.sleep(60)
    else:
        Worker(queue, handlers, queue_name=kind, lease_seconds=5).execute(lease)
        print("settled")


if __name__ == "__main__":
    main()
