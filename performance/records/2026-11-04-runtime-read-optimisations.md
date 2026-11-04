# Runtime read optimisations, 4 November 2026

Two local changes reduced database exchanges while preserving primary-state checks. Hosted
impact remains unmeasured. [Safe samples](2026-11-04-runtime-read-optimisations.json) retain the
baseline/candidate hashes and every measured observation. These follow the separate
[generation/head batching experiment](2026-11-04-postgres-read-batching.md).

## Stream snapshots

The reader now takes one committed PostgreSQL statement snapshot of the owner, run, eligible
producer and durable answer. Its shared generation lock fences restore cutovers. Conversation
rows remain available to writers during the read. An uncommitted cancellation leaves the last
committed state visible; a read after cancellation commits suppresses the draft. Scope checks,
job revision/token matching and separate delivery metadata remain enforced.

The balanced diagnostic compared the prior method against this candidate using one pooled
connection, five warmups and 100 measured reads per variant/state/delay, shuffled with seed 12.
All 840 snapshots matched exactly. A synthetic empty-cache adapter removed Redis variability;
there was no HTTP/SSE client or live inference in the timer.

| State | Delay each direction | Prior median | Candidate median |
| --- | ---: | ---: | ---: |
| Running | 0 ms | 1.347 ms | 1.030 ms |
| Running | 2 ms | 27.322 ms | 20.155 ms |
| Completed | 0 ms | 2.453 ms | 1.818 ms |
| Completed | 2 ms | 33.224 ms | 25.019 ms |

Fifteen real PostgreSQL/Redis streaming tests passed. Added cases prove that a read completes
while another transaction holds the conversation lock, rejects a committed tombstone, and
rejects a replaced database generation. Existing cancellation, stale-owner, Redis-loss,
bounded-buffer and cross-owner checks also passed. This does not qualify a user-turn percentile.

## Queue claim reads

The admission advisory lock and three independent reads now share one ordered pipeline.
Capacity selection, stable ownership recovery and tenant/resource checks keep their original
ordering. The subsequent lease writes and database-time fences are unchanged.

Thirty complete synthetic turns per variant/delay followed two warmups per variant, using a
single connection and randomized paired order. All 128 turns produced exactly one READY result.
Both variants used the already-batched generation/head reads. Median complete-turn time was
73.583 versus 72.190 ms without injected delay, and 1,363.872 versus 1,320.121 ms with 2 ms
in each direction: an additional 43.752 ms reduction in that delayed diagnostic.

The primary was also used by separate-schema quality and integration checks. Balanced ordering
reduces order bias but cannot remove that confound; these measurements are exploratory.
There were no external model calls or HTTP/SSE delivery in the measured turns. The thirty-turn
cohorts establish no qualified percentile. All 114 focused worker/recovery/release tests passed.
Full verification subsequently passed 703 tests with real services and zero skips in 136.46 s.

## Reproduction

Use the explicitly disposable PostgreSQL 17.11 instance on loopback port 55449:

```sh
PYTHONPATH=. \
  BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  uv run python scratch/group12/probe-stream-snapshot.py
PYTHONPATH=. \
  BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  uv run python scratch/group12/probe-pipelined-claim.py
```

The scripts create/remove only unique schemas and an ephemeral loopback TCP relay. Saved
baselines, scripts and results remain local under `scratch/group12/`; evidence copies are in
ignored `build-order/evidence/group12/`. Source hashes must match before reproduction. No applied
migration changed, and no hosted deployment or PERF-001–005 improvement follows from these probes.
