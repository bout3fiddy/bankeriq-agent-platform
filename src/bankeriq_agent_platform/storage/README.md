# PostgreSQL journal and synthetic job records

Implemented on 2 November 2026. `PostgresStateStore` implements the existing
`StateStore` interface and adds explicit admission, paginated journal reads,
approval decision recording and conversation tombstones. The default executable
profile still uses `InMemoryStateStore`. Inject the PostgreSQL adapter through
`open_runtime(store=...)`; model clients, permissions, gateway and continuation
resolver retain their existing dependencies. Database connections and credentials
stay in this application adapter. No cloud resources are created.

## Local reproduction

Run from the repository root with Docker and uv installed. This database contains
synthetic data only. The test suite creates unique tenant namespaces, applies
migrations idempotently and changes the recovery generation in fencing tests;
use a dedicated disposable database with no other runtime processes attached.

```sh
uv sync --locked --extra postgres
docker run --detach --name bankeriq-journal-test \
  -e POSTGRES_PASSWORD=synthetic-local -e POSTGRES_DB=bankeriq \
  -p 127.0.0.1:55432:5432 postgres:17
docker exec bankeriq-journal-test pg_isready -U postgres -d bankeriq
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic-local@127.0.0.1:55432/bankeriq'
uv run pytest tests/integration/test_postgres.py -q
uv run python scripts/verify.py
```

Wait for `pg_isready` to report accepting connections before running tests.
Without `BANKERIQ_TEST_POSTGRES_DSN`, the database tests explicitly skip. Other
runtime tests keep using local fakes and memory. To remove this disposable test
database after inspection:

```sh
docker rm --force --volumes bankeriq-journal-test
```

Trusted application composition supplies the writer DSN through its environment:

```python
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate

# Apply migrations once during release setup with the migration role.
migrate(environment["BANKERIQ_POSTGRES_DSN"])
store = PostgresStateStore(environment["BANKERIQ_POSTGRES_DSN"])
# Pass store=store to the existing open_runtime(...) composition.
```

Use separate migration/runtime roles in deployment. Production authentication,
connection pooling, private connectivity and database role provisioning remain
Azure deployment work. The adapter opens and closes one connection per operation.
It requires a read/write server with `target_session_attrs=read-write` and uses
`READ COMMITTED`; there is no replica route.

## Record and transaction mapping

- **`admit(context, request_id, items)`**
  Records committed together: Tenant/request fingerprint, permitted user events, admission sequence,
  queued run, ready/blocked coordinator job, conversation cursor and projection dirty record.

- **`save_checkpoint(...)`**
  Records committed together: Run revision/status/budgets/pending references, appended events,
  unique call outcomes and opaque delivery metadata, completed current synthetic job, pending
  approval/job records, next coordinator work, projection cursor and stable transition response.

- **Terminal checkpoint**
  Records committed together: Above writes plus release of the active run and activation of the
  earliest queued turn and its blocked job.

- **`record_approval(...)`**
  Records committed together: Exact scoped intent fingerprint, trusted decision, operation version,
  expiry and projection dirty cursor.

- **`delete_conversation(...)`**
  Records committed together: Permanent tombstone, increased deletion generation, cancelled
  runs/jobs and dirty invalidation cursor. Physical deletion is disabled.


`create_run()` is the synchronous endpoint compatibility path: `run_id` serves as
its stable request ID. Its legacy expected conversation revision does not reject
admission; execution checks both conversation and run revisions. Trusted callers
can use `admit()` when request and run identities differ. The HTTP endpoint still
applies its input policy before calling the store. Storage accepts only user
messages for admission; it cannot decide whether their contents are permitted.

The admission fingerprint covers verified tenant/user/conversation/run identity
and the exact permitted input delta. Matching requests return the existing run
status; conflicting reuse raises `StateConflictError`. Request keys are unique
within a tenant, including across conversations. Admission increments its own
sequence and journal cursor without changing execution revision. A unique partial
index enforces one active logical run per conversation. Later inputs remain
blocked and are excluded from the active run's context. Context orders complete
turns by admission sequence, then events within a turn, so an early-journalled
queued input appears after the preceding answer when its turn starts.

The coordinator returns a queued result for inactive admitted work and returns
stored status/delivery on an admission retry. Synthetic callers explicitly invoke
`Coordinator.run()` for an activated queued turn. There is no dispatcher or poller.

Checkpoints use `(tenant, run, expected_run_revision)` as a stable operation ID,
with a fingerprint of the full proposed transition. Matching replay reconstructs
the original response from a bounded run snapshot and journal cursor. Changed
content at that identity conflicts. Result uniqueness includes tenant,
conversation, run and call; each result references the original call fingerprint.
Delivery IDs are stored separately from model history. Only the gateway's
permitted result variants can enter events; raw tool payloads and exceptions
have no journal encoding. Input policy and current tool/report permissions remain
trusted application responsibilities.

Locks follow recovery-generation row, conversation head, run, then related
records. The conversation lock serializes all operations on that aggregate,
including reads spanning multiple statements; it prevents inconsistent snapshots
under `READ COMMITTED`. No transaction spans conversations. Checkpoint updates
also include conditional revision predicates. Model/tool I/O happens after the
transaction closes. Deadlocks, serialization failures and connection failures
retry database work at most three times. Every retry reconnects to the primary
and looks up the same stable identity before attempting writes. Exhaustion raises
`CommitUnknownError`; callers must retain IDs and reconcile before dispatching
more work. Admission retries cannot imply permission to repeat an external effect.

These records do not provide worker execution ownership. Concurrent matching
checkpoint retries may both observe the committed response. Keep execution
synthetic and serialize execution per run until the worker group implements
claims, leases, attempt ownership and external-effect reconciliation. A crash
while the synchronous coordinator is running preserves its checkpoint and work,
but automatic execution recovery is deferred. Approval consumption and expiry
scheduling also remain in the existing synthetic resolver; stored decisions
cannot independently authorise an action.

## Bounds, versions and retention

Numbered SQL migrations are applied transactionally under a migration lock.
`schema_versions` stores their hashes and rejects changed applied files. Checkpoint
and history-page format versions start at 1. Journal encoding uses explicit
variant tags; unsupported tags fail explicitly. Migrations 002 and 003 add call
identity and outcome fingerprint records to the initial schema. Pre-003 outcomes
can have a null fingerprint; new adapter writes require the original intent.

Individual application events are limited to 60,000 encoded bytes, deltas to
100 items/128 KiB, and database event rows to 64 KiB. Checkpoint/transition JSON
has a 256 KiB database bound. Budgets cannot decrease. The history-page table
allows at most 100 cursor positions/1 MiB per page. The
[session/history adapter](../sessions/README.md) now materialises pages at a smaller
800,000-byte application bound, with version-1 snapshots and atomic cursor progress.
Migrations 006/007 add snapshot storage, admission cursors, dirty counters and due-work
indexes. Existing `history_page()` still reads the journal; the session API prefers
materialised pages. Metadata-only transitions can leave event-sequence gaps.

Recent context selects whole turns up to 200 items/256 KiB, excluding later queued
turns from an active run. An oversized newest turn keeps its committed outcomes
and reports incomplete context; further inference fails explicitly. Semantic
compaction remains deferred. Growing transcripts are never rewritten into checkpoints.

Projection work coalesces to the latest durable cursor, including state changes
without text. The explicit projector commits bounded immutable pages, snapshots and
progress together, preserving newer dirty work. No journal pruning is implemented.
Removal requires coverage plus all active-run, result/delivery, approval, audit and
request replay retention windows. Retain stable response identities for supported
retries; backup and tool-owned report retention require separate policies.

Tombstones retain ownership and prevent conversation ID reuse, late writes and
history reads. Deletion invalidation carries an increased generation. Each adapter
pins the database's unique recovery ID; every operation checks it under a shared
lock. Checkpoints carry the recovery ID through coordinator/continuation saves,
so reconnecting an old checkpoint through a new adapter also fails. After a
restore that might lose commits, operators must pause admission/dispatch, fence
old processes, advance the recovery ID to a fresh UUID under an exclusive lock,
rebuild derived views and reconcile accepted requests/external effects before
reopening. A restored integer counter is insufficient. This release defines and
tests fencing; it does not automate cutover or backup restoration.

## Executed evidence and gaps

On 2 November 2026, local Docker PostgreSQL 17.11 (Linux ARM64) passed the PostgreSQL
suite with synthetic data, including migration installation on an empty database.
The final `uv run python scripts/verify.py` passed formatting, lint, readability,
types and all 257 Python tests with the database enabled (13 PostgreSQL tests).
Tests cover every write boundary of admission, result settlement, terminal turn
activation, approval creation/decision and tombstoning; each injected failure
leaves all related rows unchanged. Lost acknowledgements resolve by stable IDs,
including an admitting process exiting immediately after commit. Independent
processes race duplicate and distinct admissions; a new interpreter recovers the
input and ready/blocked work together. Other checks cover stale revisions,
queued-turn context/order, budgets, tenant/owner isolation, recovery fences,
bounded history cursors and the real coordinator's synthetic restricted-report
approval/job continuation without private payloads in database or model context.

No HA failover, restore, production credentials/roles, latency/throughput claims,
worker claiming/heartbeats/retries,
parallel tool execution, replicas, sharding or broker transport are implemented
or validated by this group.

The subsequent [shared prompt group](../../agent_runtime/prompts/README.md) adds
migration 004 and per-step manifest preparation to this adapter. Global prompt
bodies remain in immutable version records; prepared checkpoints retain manifests
and context cursors. Existing checkpoints without manifests remain readable.

## Durable worker extension

The [worker adapter](../workers/README.md) reuses admission/checkpoint transaction
builders from this store. Migrations 008/009 add recovery envelopes, budgets,
settlement/control identities, notifications and capacity indexes. Synchronous
checkpoint writes reject active worker-managed jobs. In-memory behaviour is unchanged.
Worker validation and current limits are recorded in its guide and build-order group;
the dated group-03 evidence above retains its original scope.

Migration 010 adds worker failure reasons, crash accounting, control revisions,
unknown-effect tracking and audited recovery requests. Current worker interfaces and
acceptance evidence are documented in the [worker guide](../workers/README.md).
