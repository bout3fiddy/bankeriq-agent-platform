# Local concurrent streaming evidence

Status: executed local evidence; hosted qualification remains pending.
The [numeric record](2026-11-04-local-concurrent-streaming.json) retains all 400 scheduled
attempts, including client backlog and rejected subscriptions. These are synthetic responses.

## Environment and workload

- Source base: `54440b894deefad6dbfefe70252bfab37013fa84`, with uncommitted group-12 changes.
  The record includes a source-content digest captured before the later idle-heartbeat fix.
- PostgreSQL 17.11 and Redis 7.4.12 in task-owned local Docker containers.
  PostgreSQL commit timestamps were enabled on this disposable primary.
- One loopback HTTP API and two independent worker processes. Each worker has two slots per
  queue; PostgreSQL limits each queue to four global/four tenant slots. Each process has a
  primary pool of four. Notifications supplement the 250-ms fallback scan.
- One synthetic user owns every conversation. Each attempt starts a fresh conversation.
  One warmup per tier is excluded. The model emits three text fragments over 120 ms.
- Batches of 1/2/5/10 arrive every 0.5/1/2.5/5 seconds: 120 offered turns/minute per tier.
  This accelerated local workload differs from the 20/minute hosted qualification envelope.
  A scheduled arrival that exceeds the declared active-client tier is recorded as backlog.
- No live model, Entra token, provider token count, Azure call or cloud change was used.

## Results

Times below are milliseconds. p95 uses nearest rank among successful initial deliveries.

| Active sessions | Successful initial deliveries | Admission p95 | First text p95 | Final p95 |
| --- | --- | --- | --- | --- |
| 1 | 94/100 scheduled | 30.6 | 428.7 | 525.7 |
| 2 | 100/100 | 47.2 | 224.3 | 329.0 |
| 5 | 100/100 | 69.2 | 518.5 | 624.9 |
| 10 | 80/100 | 121.4 | 651.5 | 743.4 |

Six serial arrivals remained client backlog; they were never submitted. Their scheduled records
remain in the evidence. At ten sessions, the eight-stream per-user limit rejected twenty initial
subscriptions. All twenty accepted runs subsequently recovered correct durable answers using
their original run IDs. Every accepted run completed correctly; recovery does not replace the
original rejected-subscription outcome or enter its latency sample.

Worker spans show four simultaneous inference executions. Across 398 inference claims,
including warmups, queue wait median/p95/max was 23/549/597 ms. The pooled queue figure mixes
tiers and is diagnostic only. It does not qualify the supported-envelope queue target.

The client bounded clock offset with an authenticated primary-timestamp round trip. The p95
upper bounds for commit-to-delivery were 50.9/52.1/61.5/71.3 ms by tier. The record retains both
ends of each interval, exact primary commit time derivation and the separate terminal-write
bound. These loopback measurements do not establish hosted delivery latency.

## Reproduction

Use disposable local services only. The workload creates a unique schema, launches its own
API/workers and removes that schema/processes at the end. It keeps local evidence on failure.

```sh
BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
BANKERIQ_TEST_REDIS_URL=redis://127.0.0.1:56399/0 \
uv run python -m tests.scaling.conversations \
  --output scratch/group12/conversations-two-workers \
  --workers 2 --slots 2 --count 100 --interval 0.5
```

Choose a new output directory for another run. Raw process logs and full ledgers remain under
the ignored output directory; the tracked JSON is an allowlisted numeric/status export.
Do not run Redis-flushing tests on the workload's Redis database during collection.

## Limits and next evidence

PERF-001 through PERF-005 remain unqualified for the live deployment. Output is shorter than
the realistic 50–100-token cohort, provider transport is synthetic, and CPU/RSS/pool/lock
sampling was not collected. The 94- and 80-success tiers have fewer than 100 comparable latency
samples. One-versus-two-process comparisons at identical capacity, live token accounting,
multi-user isolation, cross-replica reconnect, repeated compaction and the two-hour soak need
their separate evidence. No claim of production throughput or memory quality follows here.

After this collection, idle SSE received a fifteen-second keepalive comment and cancelled
runs stopped presenting earlier assistant preambles as final answers. The slow-consumer test
now permits the initial reset/saved/view snapshot before deliberately stalling its reader.
These correctness changes require their own tests; the measurements above retain their original
source digest and workload. The prepared twenty-case English/Dutch/mixed-language fixture suite
has not yet been run twice against the live model.
