# Durable synthetic workers

`PostgresQueue` executes explicitly enrolled synthetic tasks using the existing
PostgreSQL jobs, journal, checkpoints and projection records. `Worker` owns threads,
heartbeats, polling and draining. Core runtime types contain no scheduler or clients.
`open_runtime()` and the in-memory profile retain their synchronous behaviour.
The [asynchronous profile](asynchronous.md) adds model/tool dispatch and tool-group joins.

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
commit, with no database locks held. The base queue has one enrolled synthetic checkpoint task
per run; the orchestration
queue extends its settlement boundary for concurrent children.

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
access again. Expired approvals and exhausted budgets terminate runs with no uncertain effect,
releasing the next accepted turn. Unknown effects remain stopped and inspectable.
The initial run budget cannot increase in a continuation.

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
[group evidence](../../../build-order/06-completed-durable-postgresql-workers.md).
These group-06 tests did not validate HA, hosted deployment or semantic memory.
Later [Azure lifecycle evidence](../../../build-order/09-completed-azure-runtime-deployment.md)
and the [semantic memory adapter](../memory/README.md) cover those additions.
Production authentication/network policy and throughput SLOs need separate evidence.

## Failure inspection and recovery controls

Migration 010 adds fixed failure codes, process-loss counts, a control revision,
explicit unknown-effect state and an immutable recovery-command audit. The revision
changes for execution/control transitions; heartbeats do not change it. The database
sweep accounts for expired claims independently of installed handler versions. Each
lost process consumes its existing durable allowance, including loss before dispatch.
Unavailable and stopped jobs occupy no live execution lease.

`RecoveryControls` accepts a verified `RecoveryActor`, a trusted target `RunContext`
and an application `authorize(actor, context, action)` callback. Tenant scope is checked
before reading a job. Current inspection permission gates `inspect()` and `decisions()`.
Inspection returns operation identity, fixed status/reason, counters, revision,
recovery generation, unknown-effect flag and eligible actions. It omits arguments,
results, provider errors and credentials. Handler unavailability is reported relative
to the inspecting deployment; an older deployment cannot globally disable a version
that another compatible worker can execute.

For example, application composition can authorise an operator with a live policy:

```python
controls = RecoveryControls(queue, handlers, authorize=current_operator_policy)
view = controls.inspect(actor, context, job_id)
request = ControlRequest(
    request_id="retry-request-17",
    action="retry",
    expected_revision=view.revision,
    recovery_id=view.recovery_id,
)
receipt = controls.apply(actor, context, job_id, request)
```

The imports are from `agent_runtime.workers.controls`. Authentication supplies
`actor`; the request body cannot choose its identity or grant permissions. The target
context retains the conversation owner's identity. Enterprise authentication and an
operator UI remain outside this synthetic group.

Eligible `retry` commands make recoverable ready/retry/stopped work due now. They
preserve the operation identity, arguments, attempt usage, run budget and unknown-effect
flag. Unknown effects require saved and current replay permission or compatible
idempotency. A job already in reconciliation cannot switch to execution through retry.
Eligible `reconcile` commands schedule the registered lookup for the same operation ID;
they never execute the operation. Lookup requires unused run allowance even when its
execution attempt limit has been reached. Completed/cancelled/terminal work cannot
reopen, and these controls provide no budget-reset or budget-increase action.

Each command rechecks operator access, current execution permission, handler version,
current approval policy and database-time approval expiry. `Handler.approval_valid`
lets composition check current approval revocation against the saved context/arguments.
Retries retain the original persisted approval deadline. Workers repeat those checks
before dispatch or lookup, so accepting a command grants no lasting permission.
Callbacks receive detached intent snapshots. Mutation rejects the policy result and
cannot change saved arguments. Callbacks execute outside database transactions and are
not repeated by database retries; locked revision/timeline checks reject changed state
after the policy lookup.

The command's actor, request identity, action, fingerprint and fixed decision commit
with its state change. Same-ID retries return the original receipt while current
operator access permits that read. Conflicting reuse rejects; stale or denied new
requests record safe decisions. `decisions(..., limit=20)` reads a bounded audit page.
Unknown commit recovery uses the same primary-side command identity and never repeats
an external operation. Restricted data and exception text do not enter these records.

Shutdown refunds a claim only when its current owner has not started dispatch or
lookup. The refund is idempotent and does not record a process loss. In-flight handlers
continue draining with heartbeats. A hard kill follows normal crash accounting.
Unknown effects remain explicit when access, approval or budgets stop progress; the
worker preserves their operation identity instead of treating exhaustion as a known
external failure. Stopped work permits other eligible conversations/tenants to progress.

The upgrade from migration 009 conservatively retains uncertainty for legacy retry
phases whose external outcome cannot be established. No migration resets usage or
changes the saved operation identity. Local upgrade acceptance exercises that path.

## Local recovery and scaling evidence

[Group 08](../../../build-order/08-completed-recovery-and-scaling-validation.md) retains
multiprocess load, query plans, process-loss, primary outage and backup-restore evidence.
The local measured profile uses two coordinator and two tool processes, two execution
slots per queue/process, four global slots per queue and two per tenant. Lease expiry
is five seconds, heartbeat interval one-third of the lease, and idle polling 50 ms.
Synthetic calls take 10 ms; real adapters need their own latency/quota measurements.

Use `pressure(queue, kind, handlers)` from `workers.scaling` for a bounded primary-side
observation. It reports compatible due work and oldest age, excluding approval waits,
future retries, exhausted budgets, unsafe replay, unavailable lookup handlers and
held resources/tenant slots. Counts cap at 10,000 with `capped=True`; live permissions
are still checked on dispatch. Global capacity remains explicit configuration.
Scaling policy and hosting replicas belong to application infrastructure.

The measured six queue loops reserve 56 database slots including eight API/reserve
slots. Each load process uses a four-connection state pool; peak observed primary
connections were 15. Projection batches are ten chats with 50-event thresholds.
These are workload settings, not automatic production capacity recommendations.
On a failed claim connection, workers wait for their next bounded poll and reconnect;
unknown claim commits retain their durable owner/lease and operation identities.
