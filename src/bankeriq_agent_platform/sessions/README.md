# Redis sessions and asynchronous history

Implemented with real local PostgreSQL and Redis validation. PostgreSQL remains
required for reads and acceptance; Redis contains disposable permitted display data.
The default executable still uses memory. No worker, scheduler or cloud deployment
is started by this adapter.

## Composition and API

Install the optional `postgres` and `redis` extras. Trusted startup creates the
clients, authenticates users and applies the input policy:

```python
from bankeriq_agent_platform.sessions.cache import RedisSessions
from bankeriq_agent_platform.sessions.service import SessionService
from bankeriq_agent_platform.sessions.prompts import RedisPromptBodies
from bankeriq_agent_platform.storage.postgres import PostgresStateStore, migrate
from bankeriq_agent_platform.storage.prompts import PostgresPromptResolver

migrate(environment["BANKERIQ_POSTGRES_DSN"])
store = PostgresStateStore(environment["BANKERIQ_POSTGRES_DSN"])
cache = RedisSessions(environment["BANKERIQ_REDIS_URL"])
sessions = SessionService(store, cache)
# Pass store=store and sessions=sessions into the existing open_runtime(...).
# Existing authenticate, prepare_input, permissions and model dependencies remain required.
resolver = PostgresPromptResolver(dsn, scope, body_cache=RedisPromptBodies(cache))
# Pass prompts=resolver; close cache at application shutdown.
```

`scope` is the existing application/environment `PromptScope`; `dsn` is the same
primary DSN. Prompt caching is optional. It stores immutable global bodies by
scope/version/hash. Primary binding, activation, withdrawal and recovery checks
still run on every resolution. Invalid cached hashes fall back to PostgreSQL.

The authenticated invocation endpoint accepts four additional strict commands
when `sessions` is supplied:

| Action | Fields beyond `action` | Result |
| --- | --- | --- |
| `read_session` | `conversation_id`, optional `minimum_cursor` | View and provisional state |
| `read_history` | `conversation_id`, optional `after`, `limit` | Events and resume cursor |
| `stage_input` | `conversation_id`, `run_id`, `request_id`, `message` | Provisional display only |
| `admit_input` | Same as `stage_input` | Durable acceptance and stable cursor |

Create the conversation through the existing command first. Tenant/user identity
comes from authentication, never the request body. Both input commands apply the
injected input policy before caching or admission. Accepted inputs create queued
work without invoking the model. Matching retries return the original admission
sequence/cursor plus current status; conflicting reuse fails. A provisional entry
makes no durable reservation and may disappear. Reconnect reconciles entries by
request ID and fingerprint, including an explicit conflict if another input won.

## Reads, bounds and fences

Every read checks the small conversation head on the primary for ownership,
tombstones, recovery generation and cursor. An exact matching Redis view avoids
history loading. A miss hydrates a version-1 snapshot plus bounded journal tail;
a busy tail falls back to the recent journal window. Older history pages return
up to 100 events and 800,000 encoded bytes with a resume cursor. Cursor gaps from
metadata-only transitions are valid. History pages use journal sequence; recent
views order complete turns by admission sequence, then sequence within a turn.

Session keys include hashed tenant/conversation/user scope and the unique database
recovery ID. Atomic Lua updates reject lower deletion generations and cursors.
Decimal-string cursors preserve full PostgreSQL bigint precision. Explicit
cleanup also compares a per-mutation epoch and Redis-time last access. A short
refill lease is advisory: contenders can independently hydrate and use cursor CAS.
There is no waiting loop or correctness dependency on the lease.

Recent views contain at most 200 items/256 KiB. SQL returns at most 201 recent
records to detect a clipped oldest turn; query cost still needs production-scale
measurement. Older complete turns are dropped together to retain tool-call/result
pairs. Model context also reserves encoded space for instructions and current tool
schemas through `RunLimits.context_items/context_bytes`. These are JSON byte/item
bounds, not provider token estimates. An oversized newest turn commits its permitted
results, returns `context_complete=false` with empty recent events, and blocks
further inference with `ContextBudgetExceeded` (HTTP 422). Its full permitted history
remains pageable. Semantic compaction and tokenizer-specific budgets remain future work.

The new context window applies to existing retained checkpoints as well. Drain
prepared inference before upgrading when exact reproduction of the previous,
larger context window matters; automatic cross-version execution recovery belongs
to durable workers. Prompt manifests retain their existing version/hash guarantees.

At most eight provisional inputs of 32 KiB each and 32 fixed-code progress entries
are cached. Progress sequence is monotonic per conversation and supplied by trusted
application code. No arbitrary progress text, raw tool payloads, artifact identifiers
or permission decisions enter the session cache. Tool delivery metadata remains in
its existing separate durable records. UI views can include later queued inputs;
active-run model context excludes them. Redis never supplies execution authority.

## Projection and idle lifecycle

`SessionService.project_due()` performs one bounded batch per selected conversation.
Defaults are 60 seconds, 100 events, 1 MiB accumulated deltas or 15 minutes idle.
Call it from application-owned scheduling; tests invoke it explicitly. Each batch
locks recovery generation, conversation, then projection work under READ COMMITTED.
It commits an immutable page, recent snapshot and materialisation cursor together.
A page covers at most 100 cursor positions/800,000 encoded bytes. The fixed target
survives retries after an unknown commit acknowledgement. Newer input remains dirty.

After that transaction, refresh Redis and acknowledge only its applied cursor.
An outage leaves cache repair pending; repeated refresh is idempotent. Materialisation
work takes priority over cache-only repair, and batches rotate by last materialisation
time. This prevents a dead cache from starving later history writes. Redis repair
also discovers generation changes and tombstones from PostgreSQL. Direct admission
refreshes immediately; other transitions refresh on read or explicit projection.

Keys have a configurable 30-minute idle TTL, shared by explicit idle cleanup. Accepted
messages and authenticated POST /activity signals renew existing history/progress keys.
Passive history/status reads, open SSE streams and background projection do not renew
activity. A small generation-scoped marker records explicit activity without decoding
the transcript. Repeated user signals coalesce within 15 seconds. A future UI can signal
a deliberate open, typing or history action; it must not send periodic idle heartbeats.

`SessionService.keep_active()` renews existing keys for conversations with unexpired
PostgreSQL worker leases, including a waiting parent whose tool is still running.
For example, a 45-minute tool can retain a 30-minute cache through periodic renewal
even after its browser disconnects. It loads only scoped lease/conversation metadata;
missing cache payloads are rebuilt by the next ordinary read. Ready jobs, approval waits,
cancelled work and expired leases alone do not extend retention. After the last user
interaction or live-work renewal, finite TTLs continue counting down. Redis outage or memory
eviction can still remove cached data; accepted work remains recoverable from PostgreSQL.

Schedule bounded maintenance pages outside core runtime logic, with a complete sweep
well inside the configured TTL. For example, call `keep_active(limit=100, after=cursor)`
each tick, pass its returned cursor to the next tick and restart at `("", "")` after
`None`. The Azure coordinator loop does this every second, followed by `project_due()`;
tool identities need no Redis access. Larger deployments must size the page rate to
their active conversation count. No timer or thread is allocated per idle conversation.

Background publication preserves the activity deadline, including after cache loss.
An expired view is reconstructed for a passive reader without being cached again;
a recent interaction, live lease or durable event permits caching within its TTL.
Run due projection before idle cleanup to materialise the final idle window.
Explicit cleanup refuses a live conversation whose snapshot cursor still trails its head. The
bounded cleanup scan returns `(removed, next_scope)`; pass `after=next_scope` until
it returns `None`, then start the next sweep from the beginning. Keyset pagination
advances past missing Redis keys. Cleanup checks both primary inactivity and Redis
access time, the activity marker, epoch, cursor and generation. A live worker lease or
recent explicit interaction/new turn prevents removal. The Azure harness relies on TTL expiry;
its maintenance loop does not schedule explicit idle cleanup.
TTL eviction can happen earlier; retained PostgreSQL events still recover the view.

No journal pruning is implemented. Removal requires durable page/snapshot coverage,
completed run/result/delivery/approval retention, request replay windows, audit policy
and backup/report retention decisions. Tombstones retain ownership and prevent ID
reuse. After database restore, fence old processes and advance the recovery UUID
before serving traffic. Old Redis namespaces expire independently; a delayed writer
may physically recreate an old key after eviction, but primary fences prevent
serving it. This release tests generation changes, not a real backup restoration.

## Reproduce local validation

Use dedicated disposable containers with synthetic data only. Tests change recovery
generations and create/drop isolated schemas. Run from the repository root:

```sh
uv sync --locked --extra postgres --extra redis
docker run -d --name bankeriq-sessions-pg \
  -e POSTGRES_PASSWORD=synthetic -e POSTGRES_DB=bankeriq_test \
  -p 127.0.0.1:55432:5432 postgres:17
docker run -d --name bankeriq-sessions-redis \
  -p 127.0.0.1:56379:6379 redis:7-alpine
docker exec bankeriq-sessions-pg pg_isready -U postgres -d bankeriq_test
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55432/bankeriq_test'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56379/0'
uv run pytest tests/integration/test_sessions.py -q
uv run python scripts/verify.py
uv run python -m scripts.measure_sessions
```

Wait for `pg_isready` before testing. Without both environment variables, session
integration tests explicitly skip. Cleanup only these disposable containers:

```sh
docker rm -f -v bankeriq-sessions-pg bankeriq-sessions-redis
```

Coverage includes write-boundary rollback, unknown commits, fresh-process acceptance
recovery, duplicate/conflicting inputs, tenant/owner isolation, restricted outputs,
cache loss/outage, concurrent hydration, stale cursors, deletion/recovery fences,
idle races, projection acknowledgement loss, 1,100 busy-conversation events, trigger
thresholds, oversized context, fairness, pagination and prompt-cache corruption.
The measurement script reports local warm/cold/page timings and projection latency;
these are exploratory samples, with no production SLO or threshold tuning claim.

Production TLS/authentication/roles, Redis capacity/eviction pressure, HA failover,
real backup restoration, worker ownership/heartbeats/retries, streaming transport,
parallel tool execution, replicas and sharding remain unvalidated or deferred.

## Executed evidence (3 November 2026)

`uv run python scripts/verify.py` passed all 324 tests and static checks with both
services enabled: PostgreSQL 17.11 and Redis 7.4.12 on local Docker/Linux ARM64.
This includes 21 session integration scenarios and two pure context-selection tests.
The rebuilt synthetic container passed offline workflow, token, readiness, non-root
HTTP, two-turn and shutdown checks. Wheel inspection confirmed packaged Lua and
migrations 006/007, with validation fixtures excluded.

The 120-event measurement ran 20 samples per read mode. Median/maximum times were
6.13/7.20 ms for warm reads, 23.27/25.58 ms for cold reads and 8.74/10.55 ms for pages.
Two projection batches took 32.09 ms total. These measurements include local primary
connections; they do not establish deployment capacity or justify tuning the defaults.

## Worker maintenance integration

The [durable synthetic worker](../workers/README.md) now accepts a bounded maintenance
callback for `project_due()` and idle cleanup. PostgreSQL retains dirty work across
worker/Redis loss. The dated session-group evidence above predates worker ownership;
worker failure tests and limits are recorded in the worker guide.

## Bounded progress reads

Progress uses a separate Redis key scoped by recovery and deletion generation. Its
32-entry document has its own TTL and run/attempt/sequence fencing. Token writes and
SSE metadata polls never deserialize the cached transcript or reconstruct a checkpoint.
Durable history remains cursor-paginated; child status and separate delivery metadata
come from primary reads. Idle cleanup evicts the history view; the progress key expires
on its TTL after activity stops. Explicit activity and live-lease maintenance extend
both existing payloads through Redis expiry commands. Deleted/old-generation keys cannot
be served by current readers.

[Group 08 measurements](../../../build-order/08-completed-recovery-and-scaling-validation.md)
compare fixed-size progress updates at 10 and 3,000 retained turns, including SQL reads,
WAL, Redis traffic, CPU and memory. This does not bound durable database growth; storage
retention and capacity controls remain planned in group 13.

## Activity retention evidence (3 November 2026)

Local integration coverage uses PostgreSQL 17 and Redis 7. It exercises retention across
multiple one-second cache lifetimes while an independent tool process renews its lease,
expiry after that process is killed, passive SSE/status reads through expiry, explicit
activity, idle-cleanup races, bounded pagination, Redis outage, tenant checks and recovery
fencing. Completed, cancelled, expired, approval-waiting and deleted work stop renewal.
Passive history reads and background publication cannot resurrect an idle cache.
Accepted history remains reconstructable after expiry. The earlier 488-test iteration
renewed on passive progress polls; the current implementation corrects that behavior.
Current executed checks and hosted evidence are recorded in the
[persistent deployment guide](../../../infra/azure/LIVE.md).

```sh
docker run -d --name bankeriq-live-test-postgres \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55439:5432 postgres:17
docker run -d --name bankeriq-live-test-redis -p 127.0.0.1:56389:6379 redis:7
docker exec bankeriq-live-test-postgres pg_isready -U postgres
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55439/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56389/0'
uv run pytest tests/integration/test_session_retention.py -q
uv run python scripts/verify.py
git diff --check
docker rm -f -v bankeriq-live-test-postgres bankeriq-live-test-redis
```
