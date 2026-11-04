# PostgreSQL read batching, 4 November 2026

Batching generation/head reads reduced median synthetic queued-turn time by 89 ms with
an injected 2 ms delay in each network direction. With no injected delay, the reduction
was 2 ms. This is a local diagnostic improvement; hosted latency remains unvalidated.
[Safe samples](2026-11-04-postgres-read-batching.json) retain all measurements and source hashes.

## Boundary and evidence

Each measured turn ran ordinary admission, preparation and inference claims, execution and
settlement against real PostgreSQL 17.11, ending with one correct durable READY answer.
The model was synthetic and immediate. There was no HTTP/SSE delivery or external model call.
Conversation creation and final correctness inspection were outside the timer. A loopback TCP
relay applied either zero delay or 2 ms per read in each direction; this simulates network
cost without claiming to reproduce Azure networking.

Both variants shared one primary connection. Each condition had two warmups and thirty measured
turns per variant, with deterministic shuffled pair order (seed 12). All 128 turns completed
correctly. Python was 3.14.8, psycopg 3.3.6 and libpq 18.6. Other local validation also used the
primary with separate state; this is a balanced exploratory comparison rather than a complete
isolated-load qualification. No 100-sample percentile claim is made.

| Injected delay per direction | Baseline median | Batched median | Reduction |
| --- | ---: | ---: | ---: |
| 0 ms | 45.914 ms | 43.887 ms | 2.027 ms, 4.4% |
| 2 ms | 1,462.987 ms | 1,373.689 ms | 89.298 ms, 6.1% |

The baseline PostgreSQL module matches HEAD
`54440b894deefad6dbfefe70252bfab37013fa84`. Other group-12 worker changes were held constant.
The candidate keeps both SQL checks and batches their transport with psycopg pipeline mode.
PostgreSQL executes pipeline statements in submission order; see
[the psycopg documentation][pipeline]. The shared generation lock still precedes the head lock,
and both results are validated before returning data or performing a write. A generation
predicate also prevents an obsolete process waiting on a locked conversation before reporting
its recovery conflict. Subsequent run/job locks and database-time lease fences are unchanged.

Ninety-three focused real-service storage/worker/recovery tests passed, including a new held-lock
case: an old generation fails with its recovery conflict, while a valid generation still waits
on the same conversation lock. The broader final verification is recorded in the local group.
The synchronous in-memory profile and all applied migrations are unchanged.

## Reproduction

The exploratory scripts and exact baseline snapshot remain local:

```sh
PYTHONPATH=. \
  BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  uv run python scratch/group12/probe-pipelined-head-confirmed.py
```

`probe-pipelined-head.py` records the preliminary prototype. The confirmed script compares
`postgres-before-head-pipeline.py` against the current implementation, including its generation
predicate. Both create/remove only their unique schema, use the loopback service on port 55449,
and choose an ephemeral proxy port. Logs and raw samples are under `scratch/group12/`; copies
are retained in ignored `build-order/evidence/group12/`. Recheck hashes before reproducing.
This optimisation remains local and does not claim that PERF-002 now passes.

[pipeline]: https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html
