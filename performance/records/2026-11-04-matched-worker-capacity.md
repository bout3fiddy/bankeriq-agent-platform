# Local worker comparison at fixed capacity

Recorded on 4 November 2026. Both configurations use four global/per-tenant slots per queue,
one loopback API, real PostgreSQL 17.11 and Redis 7.4.12, and a synthetic three-chunk model with
120 ms total delay. One configuration has one worker process with four slots; the other has
two independent processes with two slots each. Each process has a four-connection primary pool.
The [measurements](2026-11-04-matched-worker-capacity.json) retain every scheduled attempt,
separate warmups, reconnect outcomes and five-second resource samples.

## Results

Each row offered 100 measured attempts at 120/minute, with one separate warmup. This accelerated
local workload differs from the hosted 20/minute, 50–100-token qualification workload. Completion
percentiles use correct initial deliveries only; median is p50 and nearest rank is p95.

| Sessions | Workers | Initial correct | Client backlog | Recovered | p50 / p95, ms |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | 76/100 | 24 | 0 | 269 / 557 |
| 1 | 2 | 85/100 | 15 | 0 | 267 / 558 |
| 2 | 1 | 100/100 | 0 | 0 | 265 / 339 |
| 2 | 2 | 100/100 | 0 | 0 | 268 / 328 |
| 5 | 1 | 100/100 | 0 | 0 | 333 / 588 |
| 5 | 2 | 100/100 | 0 | 0 | 306 / 614 |
| 10 | 1 | 80/100 | 0 | 20 | 538 / 763 |
| 10 | 2 | 80/100 | 0 | 20 | 632 / 726 |

The serial tier exposes scheduled arrivals that occurred before the preceding request finished;
they were recorded as client backlog and never submitted. At ten sessions, the eight-stream
per-user/process limit rejected twenty subscriptions in each run after admission. All forty
recovered their original durable answers using the same run IDs. Every accepted request completed
correctly; no failed initial delivery was silently moved into the initial latency denominator.

Splitting the workers did not produce a consistent latency improvement in this single pair.
Worker pool wait counters totaled 7,055 ms for one process versus 659 + 694 ms across two.
API pool waits were similar: 9,243 versus 8,859 ms. These are cumulative wait counters over the
whole workload, including setup; they are not per-request latency or a lock-wait distribution.

## Resources

Five-second fixture samples record current RSS via `ps`, cumulative process CPU time, Python
thread/async-task counts and psycopg pool statistics. The final samples follow eleven seconds
without workload or streams. Sampling belongs to the test fixture and is absent from hosted code.

- With one worker, drained API/worker async tasks were 3/5. Pools had no waiting requests.
  Final RSS was 71,248/50,080 KiB; recorded peaks were 179,184/178,592 KiB.
- With two workers, drained API/worker/worker tasks were 3/5/5. Pools had no waiting requests.
  Final RSS was 86,784/49,520/52,544 KiB; peaks were 177,328/177,312/177,376 KiB.
- All pools remained within their configured four connections. These short runs establish no
  multi-hour memory-leak claim. Persistent pools and executor threads may remain after work drains.

## Reproduction and limits

Run sequentially after service verification, using disposable services only:

```sh
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56399/0'
uv run python -m tests.scaling.conversations --workers 1 --slots 4 --count 100 \
  --interval 0.5 --output scratch/group12/matched-one-worker
uv run python -m tests.scaling.conversations --workers 2 --slots 2 --count 100 \
  --interval 0.5 --output scratch/group12/matched-two-workers
```

The runner creates and drops its own schema and terminates only its own child processes. Do not
run Redis-flushing tests alongside it. Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84`
with uncommitted group-12 changes; the JSON includes a runtime-source hash and platform/version.
The arrival harness gained unused campaign-control callbacks between runs; runtime execution
and workload settings were identical. Other development/type-check processes ran on this shared
workstation, so this pair supports an observed comparison rather than a causal speedup claim.

No provider transport, real token generation, hosted scaling, database lock sampling or
statistical confidence interval is included. Increased total capacity, multi-tenant fairness
and hosted performance gates require their own evidence.
