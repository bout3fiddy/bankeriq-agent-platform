# Worker scheduling diagnostics, 4 November 2026

Two independent local changes improved five-session burst completion when database transport
had injected latency. Waking on a finished execution saved a median 144 ms per paired burst;
increasing preparation slots from two to five then saved 351 ms. Both changes were included
in the later revision-12 release.
[All samples](2026-11-04-worker-scheduling.json) retain the separate experiments and source hashes.
These results support hosted qualification, without establishing a cloud speedup or target pass.

## Controlled boundary

Each experiment used real PostgreSQL 17.11, one four-connection pool, five inference slots and
an immediate synthetic READY model. Five fresh conversations were admitted together. Timing
covers admission through inference settlement; setup, startup, final inspection and shutdown
are excluded. Both variants retain durable queues, locks, budgets and ownership checks.
There is no HTTP/SSE, Redis or live inference in this diagnostic.

Each delay condition has one warmup burst per variant and fifteen measured shuffled pairs
(seed 12). A burst ends when all five turns finish; correctness is checked from PostgreSQL.
All 320 turns per experiment completed correctly. The loopback TCP relay adds zero or 2 ms
per read in each network direction; it does not reproduce every Azure networking condition.
Other local tests shared the primary under separate schemas. These small diagnostic samples
do not qualify a percentile. Median paired savings differ from differences of marginal medians.

| Experiment, delay each direction | Baseline burst median | Candidate burst median |
| --- | ---: | ---: |
| Completion wake, 0 ms | 227.95 ms | 233.14 ms |
| Completion wake, 2 ms | 2.564 s | 2.441 s |
| Two/five preparation slots, 0 ms | 227.22 ms | 234.99 ms |
| Two/five preparation slots, 2 ms | 2.448 s | 2.080 s |

The completion change improved 13/15 delayed pairs, with a 144-ms median paired saving.
It does not improve the fast-loopback median. The scheduler now waits for execution completion
or its one existing listener task; it never starts a second socket wait while one is active.
A real-primary regression holds that listener open and verifies that two sequential jobs use
one execution slot before the listener returns. Shutdown drains the wait before closing it.
Initial fixture failures used invalid lifecycle/continuation states; corrected fixture execution
passed and all logs remain in scratch.

The capacity comparison holds the improved scheduler and ownership reads constant. All 15
delayed pairs improved, with a 351-ms median paired saving. Preparation concurrency becomes five
in the pilot composition; inference stays five and the pool stays four. Earlier fast-loopback
capacity results are retained in the [original record](2026-11-04-preparation-capacity.md).
The delayed workload supplies new evidence for testing this change on the hosted primary.

## Reproduction

```sh
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/postgres'
PYTHONPATH=. uv run python scratch/group12/probe-worker-completion-wake.py
PYTHONPATH=. uv run python scratch/group12/probe-delayed-preparation-capacity.py
```

Exploratory source snapshots, initial failed startup log and raw numeric outputs remain in
ignored scratch/group12. Recheck source hashes before reproduction. The first compares only
run_async scheduling; the second varies only preparation slots. The workload starts independent
async worker loops in one process. Separate process fairness/recovery and HTTP/SSE evidence
remain in the other group-12 records. Neither experiment allocates cloud resources or model calls.

## Active memory source reuse and hosted read diagnostic

SemanticStore.read previously loaded the active turn twice: once for immediate context and
again when it appeared in the bounded history window. The candidate reuses that same validated
source under the existing conversation lock. Prepared cursors, partial-summary boundaries,
source-size limits and approval/dispatch evidence are unchanged. Three redundant reads disappear
for a fresh turn. The separate single-turn comparison has two warmups and thirty measured pairs
per delay condition, using one pooled connection and the immediate synthetic model.
All 128 turns completed correctly. Delayed medians were 1.284/1.249 seconds; 23/30 pairs improved,
with a 43-ms median paired saving. Fast-loopback medians were 41.98/42.32 ms. No hosted improvement
is claimed. Existing semantic, partial-turn, approval and recovery tests validate source behavior.

The finite read-only hosted probe issued one warmup and fifty SELECT 1 statements from the
existing coordinator to its primary. Median was 1.245 ms and nearest-rank p95 3.291 ms.
It read no application data and made no model call. Connection/identity startup is excluded.
These small diagnostic samples do not qualify an application latency percentile. They also
show that the injected 2-ms-each-direction experiment should not be equated with actual Azure RTT.

```sh
PYTHONPATH=. uv run python scratch/group12/probe-memory-active-reuse.py
PYTHONPATH=. uv run python scratch/group12/probe-hosted-database-rtt.py
```

The local probe uses the disposable primary DSN above. The hosted probe uses the existing
operator-authorised exec channel, verified PostgreSQL TLS and managed identity inside the
container; credentials stay remote. The helper closed after its finite read-only measurement.
