# Ownership read batching, 4 November 2026

Batching the run, job and final expiry reads improved the local synthetic queued-turn median
from 1.350 to 1.276 seconds with an injected 2 ms delay in each network direction. Twenty-eight
of thirty paired comparisons improved; median paired saving was 55 ms. Without added delay,
medians were 44.36/43.59 ms and seventeen pairs improved. These are local diagnostics;
[all measurements](2026-11-04-ownership-read-batching.json) are retained.
Hosted impact is unmeasured.

The public boundary includes durable admission, preparation and inference claims, execution and
settlement against PostgreSQL 17.11. An immediate synthetic model returns READY. Conversation
creation and final correctness inspection are outside timing; HTTP/SSE/Redis and live inference
are absent. Both variants use one pooled connection, two warmups per delay condition and thirty
measured pairs in shuffled order with seed 12. All 128 turns completed with the expected answer.
Other local validation shared the primary under separate schemas; no percentile claim is made.

The candidate keeps generation/conversation locking first. It then pipelines the run lock,
scoped job lock and database-time expiry check in their original order. PostgreSQL executes
these statements in order; the expiry statement sees time after any job-lock wait. Every result
is checked before permitting a mutation. An expired or superseded owner cannot publish output.
A new real-primary contention test holds the job lock, observes the competing backend's actual
lock wait, expires the lease and verifies rejection after release with no transition committed.
All fifty worker tests and full verification passed: 708 tests, zero skips, 150.16 seconds.

Executed commands and preserved scratch snapshots:

```sh
PYTHONPATH=. \
  BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  uv run python scratch/group12/probe-pipelined-owned.py
BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  BANKERIQ_TEST_REDIS_URL=redis://127.0.0.1:56399/0 \
  uv run python scripts/verify.py
```

The probe compares `queue-before-owned-batching.py` with
`queue-owned-batching-prototype.py`, changing only `_owned`; all other source is held constant.
Source hashes are retained in the JSON record. The candidate function is now applied locally.
The loopback TCP relay is an experimental delay model and does not measure Azure network RTT.
This change does not establish compliance with the 500-ms overhead target.
