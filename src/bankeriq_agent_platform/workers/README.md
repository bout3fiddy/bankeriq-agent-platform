# Durable synthetic workers

`PostgresQueue` executes explicitly enrolled synthetic tasks using the existing
PostgreSQL jobs, journal, checkpoints and projection records. `Worker` owns threads,
heartbeats, polling and draining. Core runtime types contain no scheduler or clients.
`open_runtime()` and the in-memory profile retain their synchronous behaviour.
Real asynchronous model/tool dispatch and tool-group joins belong to group 07.

## Admission and reconstruction

Use `queue.admit(context, request_id=..., items=..., state=Recovery(...))` to commit
policy-approved user input and recovery state together. The application must verify
identity, permissions, permitted input and final operation arguments before admission.
`attach()` enrolls an existing synthetic coordinator job; new applications should
prefer atomic admission. Matching request/recovery fingerprints deduplicate retries;
changed input, handler version, arguments or operation identity conflicts.

`Recovery` format 1 records handler kind/version, phase, final permitted arguments or
opaque references, operation ID, replay policy and budgets. Attempt counters, due
and approval deadlines, ownership and run-wide usage live alongside it in PostgreSQL.
The adapter rejects unsupported values, non-string object keys, non-finite numbers,
unknown fields/versions and recovery JSON over 60,000 bytes before writing it.
No pickle, dynamic imports, serialized callables, credentials or local handles are used.

Register compatible `(kind, version)` entries in `Handlers`. An unavailable version
stays queued until compatible code is supplied. Retain registrations for supported
persisted work during rolling upgrades. A handler receives freshly reconstructed
state and dependencies; the lost process's stack is never used for recovery.
Handlers enforce their business argument and output schemas. Raw restricted output
must pass through the existing gateway/output enforcement before returning `Completion`.
Only approved receipts and separate opaque delivery references enter settlement.

## Claims, retries and outcomes

Configure each queue with `configure(kind, concurrency=..., tenant_concurrency=...)`.
Conflicting configurations fail. A short database advisory lock serializes capacity
reservation across processes. Indexed due/expired scans and `SKIP LOCKED` conversation
locks select work; conversation, run and job locks follow the journal's order.
Queue and per-queue tenant capacity count unexpired leases. All execution occurs after
commit, with no database locks held. This implementation has one enrolled synthetic
checkpoint task per run; concurrent real tool groups remain group 07.

Each claim increments its attempt and ownership token and uses a unique owner ID.
Database time controls expiry, retry deadlines and renewal. Heartbeats cannot revive
an expired lease. Settlement checks the token, owner, timeline, deletion generation
and revisions, then checks expiry again after its writes. A stale worker cannot commit
output. Expired dispatches require compatible idempotency support or both the saved
and current replay permissions before another execution.

`prepare()` commits dispatch phase with the saved operation ID and arguments before
calling a handler. `settle()` commits the enforced output, completed job, checkpoint,
next task and durable projection work together. It reuses `checkpoint_change()`;
`StateStore.save_checkpoint()` cannot bypass an enrolled worker's ownership.
The result fingerprint and original bounded response resolve unknown commits on the
primary. Database retries receive the captured completion and never call the handler.
Claim retries recover their original owner ID instead of reserving another job.

The scheduler retries safe execution failures with exponential backoff, starting at
one second and capped at 300 seconds. `pause()` also supports explicit retry and
approval deadlines up to 30 days. Approval binds the exact saved state and version;
matching decision retries are idempotent. Every resumed execution checks current
access again. Expired approvals and exhausted budgets terminate the run and release
the next accepted turn. The initial run budget cannot increase in a continuation.

Unknown unsafe outcomes enter `reconcile`. A registered reconciliation callback
looks up the same operation ID using fresh provider dependencies. It may return a
proven completion or leave the job unresolved; it never authorizes blind replay.
Lookups are bounded by the persisted run budget and use a one-second retry deadline.
Missing lookup support or an exhausted reconciliation budget requires operator review;
the unknown record remains durable. There is no automated compensation or claim of
exactly-once external effects.

Cancellation fences the active owner and commits a terminal run. A previously
prepared dispatch becomes `cancelled_unknown`, retaining its operation identity for
provider lookup/operator review. Cancellation cannot undo an external effect and
never automatically replays it. Deletion also fences leased/retrying/waiting work.
Recovery-generation changes reject old leases even through a newly opened adapter.

## Notification, capacity and shutdown

`Listener` owns a dedicated autocommit `LISTEN runtime_jobs` connection. Transactional
notifications contain only job IDs; receivers always scan the primary. Duplicate,
lost or malformed wake hints cannot supply execution data. Failed listener sessions
are closed and re-established. Bounded polling recovers ready work, retries, approval
expiry and expired leases even with no listener or Redis available.

`Worker` bounds local execution with a thread pool. Its configured connection budget
reserves `api_reserve` slots and checks
`process_limit * (2 * concurrency + 2) <= connection_budget - api_reserve`.
Each handler slot allows one transaction and one heartbeat; each process additionally
allows claim and listener connections. Deployment must enforce `process_limit` and
include any separately hosted workers in that budget. This sizing guard does not set
a PostgreSQL role connection limit or dynamically restrict arbitrary application clients.

Optional `maintenance` shares the same bounded executor, one task at a time per
process. For example, supply a callback that calls
`session_service.project_due(ProjectionPolicy(events=100), limit=10)` and bounded idle
cleanup. Projection is independently idempotent and remains dirty after Redis failure.
Maintenance callback limits and timeouts belong to trusted application composition.

A stop event prevents further dispatch and drains active handlers with heartbeats
running. The hosting process supplies its final shutdown deadline; forced termination
then relies on durable lease recovery. Production adapters must supply finite I/O
timeouts. This group uses only synthetic handlers and makes no hosted-drain claim.

## Reproduce local validation

From the repository root, use disposable local containers:

```sh
docker run -d --name bankeriq-workers-postgres \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55436:5432 postgres:17
docker run -d --name bankeriq-workers-redis \
  -p 127.0.0.1:56386:6379 redis:7
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55436/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56386/0'
docker exec bankeriq-workers-postgres pg_isready -U postgres
uv run pytest -q tests/integration/test_workers.py
uv run python scripts/verify.py
git diff --check
```

Tests create and remove isolated PostgreSQL schemas. Redis is disposable and tests
clear its selected database. Never point these variables at retained application data.
`tests/integration/worker_process.py` supplies independent child processes with fresh
handler registrations and no model/tool network calls. Omitted service variables
cause explicit skips and do not establish the real-service acceptance gates.

After validation, remove only these disposable containers:

```sh
docker rm -f bankeriq-workers-postgres bankeriq-workers-redis
```

Executed counts and service versions are recorded in the
[group evidence](../../../build-order/06-in-progress-durable-postgresql-workers.md).
HA failover, restoration, production authentication/network isolation, throughput SLOs,
cloud hosting, semantic compaction and real asynchronous orchestration remain deferred.
