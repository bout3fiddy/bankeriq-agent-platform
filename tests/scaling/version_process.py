"""A release fixture supporting one explicitly selected operation version."""

import os
import sys
from pathlib import Path

from agent_harness.runs.state import RunStatus
from agent_runtime.storage.postgres import PostgresStateStore
from agent_runtime.workers.queue import PostgresQueue
from agent_runtime.workers.runner import Worker
from agent_runtime.workers.state import Completion, Handler, Handlers, Recovery

version = int(sys.argv[1])
store = PostgresStateStore(os.environ["BANKERIQ_TEST_POSTGRES_DSN"])
queue = PostgresQueue(store)


def complete(context, state):
    if state.arguments.get("final"):
        return Completion(RunStatus.COMPLETED)

    return Completion(
        RunStatus.RUNNING,
        next_task=Recovery(
            kind="versioned",
            version=version,
            operation_id=context.run_id + "-final",
            idempotent=True,
            arguments={"final": True},
        ),
    )


handlers = Handlers({("versioned", version): Handler(lambda c, s: True, complete, idempotent=True)})
count = 0

for _ in range(100):
    lease = queue.claim("versioned", handlers)

    if lease is None:
        break

    Worker(queue, handlers, queue_name="versioned").execute(lease)
    count += 1

Path(sys.argv[2]).write_text(str(count))
