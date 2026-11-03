# Group 08 local evidence

Executed on 3 November 2026 on macOS/Apple Silicon, Python 3.14.8, PostgreSQL 17.11,
Redis 7.4.12 and psycopg-pool 3.3.3. All models, tools, identities and provider receipts
are synthetic. API requests use local ASGI transports in independent API processes.
No cloud resources or production services are involved.

## Reproduction

Run from the repository root. These commands create disposable local services. The
outage script stops/restarts the explicitly named PostgreSQL container. The restore
script creates/drops two temporary databases and clears the selected Redis database.
Run the scripts sequentially, with no other workload on these services.

```sh
uv sync --locked
docker run -d --name bankeriq-group08-postgres \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55438:5432 postgres:17 \
  -c shared_preload_libraries=pg_stat_statements -c track_io_timing=on
docker run -d --name bankeriq-group08-redis \
  -p 127.0.0.1:56388:6379 redis:7
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55438/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56388/0'
docker exec bankeriq-group08-postgres pg_isready -U postgres
uv run python -m tests.scaling.progress_probe --output /tmp/progress-after.json
uv run python -m tests.scaling.progress_io /tmp/progress-io.json
uv run python -m tests.scaling.load --turns 400 --output /tmp/load.json
uv run python -m tests.scaling.summarize /tmp/load.json /tmp/load-summary.json
uv run python -m tests.scaling.restore --container bankeriq-group08-postgres \
  --output /tmp/restore.json
uv run python -m tests.scaling.outage --container bankeriq-group08-postgres \
  --output /tmp/outage.json
uv run python scripts/verify.py
git diff --check
```

The executed commands used this directory as their output destination. Outputs in
`/tmp` above preserve the recorded evidence during reproduction. The probe scripts
create isolated schemas and remove them after successful or failed runs. A host-side
connection failure can prevent cleanup; remove only the disposable containers below.

```sh
docker rm -f bankeriq-group08-postgres bankeriq-group08-redis
```

## Baseline and comparison

[workload.md](workload.md) records limits declared before acceptance. The immutable
[progress baseline](progress-before.json) used commit
`1b5ea489062e683211abde73f1a251cd2aa45f69` with the same newly added
`tests/scaling/progress_probe.py` copied into that checkout. To reproduce the baseline,
create a detached checkout of that commit, copy `tests/scaling/__init__.py` and
`tests/scaling/progress_probe.py`, run `uv sync --locked`, then execute the probe with
the same environment. Do not run baseline and candidate measurements concurrently.

The matched [progress comparison](progress-after.json) predates optional pooling;
both timing runs used one-shot connections and Python allocation tracing. Median
progress latency at 3,000 retained turns fell from 243.9 ms to 19.1 ms, and repeated
history reconstruction calls fell from two per update to zero. At ten turns the new
median was 21.1 ms; both sizes passed the declared 100 ms and 2x limits.

[Supplemental I/O measurements](progress-io.json) use pooling and no allocation tracing.
They are separate from that timing comparison: each 100-update run issued 11 primary
SELECTs and 324 WAL bytes per update at both history sizes. Shared-buffer hits were
16.0/22.0, physical reads zero, and Redis inbound traffic 679/681 bytes per update.
Each run retained exactly 32 progress entries. WAL includes row-lock bookkeeping;
these observations do not claim that polling is write-free.

## Retained artifacts

- [load.json](load.json): every sampled queue, connection count, projection lag and
  process RSS; per-process CPU/pool statistics and admission latencies; table sizes,
  WAL volume, warm/cold read latencies and full EXPLAIN ANALYZE/BUFFERS plans.
- [load-summary.json](load-summary.json): admission including commit, early/late tenant
  progress, memory deltas, queue age and resource peaks with executable assertions.
- [restore.json](restore.json): real pg_dump/pg_restore, generation fencing, external
  effect lookup, acceptance-ledger recovery and Redis reconstruction.
- [outage.json](outage.json): independent worker survival, rejected outage admission,
  same-request recovery and graceful drain after actual PostgreSQL stop/start.

Long EXPLAIN predicate strings are represented as `{"text_chunks": [...]}`. Joining
those chunks without a separator restores the original string; plan nodes, counts,
actual rows, costs and buffer measurements are unchanged. Evidence is descriptive,
not a portable hardware performance promise.

## Initial failures and limits

The baseline suite had 427 passing tests and one timing-sensitive SSE test: a single
SKIP LOCKED claim could return no lease while the reader briefly held the conversation
lock. That test now retries within a bounded deadline, matching the worker's polling
semantics. A first reused-chat load fixture reused old model call IDs; the fixture now
emits distinct calls and scopes its response to the current turn.

Unpooled sustained load attempts lost host-side connections while PostgreSQL still
accepted connections inside Docker. PostgreSQL logs showed no server crash. Pooling
removed the failure in the accepted workload; the exact host/proxy failure mechanism
was not established. One test invocation overlapping an unsuccessful load had related
connection failures; it is excluded from successful validation evidence.

This is a 400-turn/2,800-job local regression, not a production throughput or soak SLO.
Queued reused conversations serialize, so first-token tails include earlier accepted
turns. The external-effect and accepted-request ledgers in the restore rehearsal are
synthetic fixtures; production operators need durable provider receipts and an actual
acceptance ledger beyond the backup point. Restore is a manual paused-dispatch procedure.
Optional replica routing remains disabled; controlled-lag validation is required before
adding it. Azure HA failover and hosted drain remain separate deployment gates.
Physical retention, storage quotas, WAL/backup policies and capacity alerts remain in
planned group 13. No durable evidence is pruned by this implementation.
