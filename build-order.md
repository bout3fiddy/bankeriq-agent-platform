# BankerIQ build order

Build from core runtime logic towards external integrations. These steps are
planned except where marked implemented. Keep external I/O mocked until the adapter steps.

Work one component at a time. Each step includes its implementation, meaningful
tests and supporting documentation before the next step begins. Reuse the existing
application composition and keep unfinished integrations explicitly labelled.

Task groups run in this order: initial runtime → containerisation → PostgreSQL
journal and job records → shared system prompts → Redis sessions and asynchronous
history → durable workers → asynchronous coordinator and tools → recovery and
scaling validation → Azure runtime deployment. Each group has its own task
numbering. Redis sessions and asynchronous history are next.

The selected stack is PostgreSQL plus ephemeral Redis. PostgreSQL contains durable
conversation deltas, history snapshots and the message queue. Redis serves active
UI views through the API; large history writes happen asynchronously and on idle.
Accepted work always has a recoverable PostgreSQL input record. See the
[full system design](ARCHITECTURE.md#selected-state-and-execution-services).
The [transaction and replication boundaries](ARCHITECTURE.md#transaction-and-replication-boundaries)
define atomic writes, primary-only reads and recovery requirements for the groups
below. The PostgreSQL journal adapter and its local failure tests are implemented;
later worker, cache and deployment guarantees remain pending.
DTS and separate brokers are outside this sequence. Tools own confidential Blob
reports and authenticated downloads. Container Apps is proposed for worker hosting;
the earlier Foundry Hosted Agents target remains a separate compatibility decision.

## Initial agent runtime

Status: complete.

Steps 1–13 were completed on 2 November 2026 and merged into `main` at `2fa515b`,
including live-model validation with synthetic integrations. Preserve this milestone
while adding infrastructure on `codex/runtime-infrastructure`.

1. **Core data types (implemented)** — Run context, messages, tool definitions, calls, results,
   output policies and run statuses.
2. **Tool registry (implemented)** — Register versioned definitions and schemas; discover tools
   filtered by permissions.
3. **Output enforcement (completed outputs implemented)** — Produce model-visible results or
restricted receipts;
   sanitise failures and route artifact references separately.
4. **Tool gateway (synchronous dispatch implemented)** — Validate calls, check permissions and
approvals, dispatch
   through a mock executor, and enforce output policy.
5. **Conversation and run state (implemented)** — Define storage interfaces and an in-memory
   implementation for history, pending calls, checkpoints and waiting states.
6. **Coordinator loop (implemented)** — Assemble context, call the selected model, handle tool
   requests and finish or suspend within execution limits.
7. **Durable continuation handling (implemented with mocked providers)** — Under
   `src/agent_runtime/durable/`, match approval/job events to pending work, claim resolution,
   reject duplicates and resume after
   all outstanding calls resolve. Preserve budgets across waits. Durable recovery
   remains future work.
8. **End-to-end test harness (implemented with synthetic I/O)** — `tests/integration/`
   connects the runtime components through visible and restricted outputs, denied
   calls, failures, cancellation, waiting and continuation. Checks actual model
   request fields and stored history for synthetic restricted payloads and artifact IDs.
9. **Foundry model adapter (implemented; local and live tests)** —
`src/agent_runtime/providers/foundry/` maps
   permitted history and discovered tools to v1 Chat Completions through the
   OpenAI SDK, then validates answers and function calls. Live Azure inference
   passed with regional `gpt-4o` version `2024-11-20` in Sweden Central.
10. **Hosted-agent adapter (implemented; local ASGI tests)** — `src/agent_runtime/hosting/` handles
    validated application commands; `src/agent_runtime/providers/foundry/hosting.py` exposes them
    through
    Microsoft's Invocations SDK. Authentication and input admission are required
    injected dependencies, supplied by local fakes in tests. Azure identity
    integration, deployment and lifecycle validation remain pending.
11. **Provider selection (implemented; local tests)** — Keep SDK integrations
    under `src/agent_runtime/providers/`; select model and hosting factories independently at
    application startup. Foundry is the initial implementation. Architecture
    tests enforce import boundaries, and provider SDKs are optional dependencies.
12. **Executable startup (implemented; local process tests)** —
    `uv run bankeriq-runtime serve` assembles mock tools, in-memory storage,
    continuation fakes and explicit test authentication/input policy through
    `open_runtime()`. `uv run bankeriq-runtime validate` checks the synthetic
    workflow. Tests launch a fresh HTTP server, complete two turns and shut it down.
13. **Live model validation (passed, 2 November 2026)** — Terraform under
    `infra/azure/` supports EU Data Zone and regional deployments. The regional
    Sweden test used renewable Azure CLI credentials and completed four model
    calls, three successive synthetic tool calls, restricted-output enforcement,
    one job continuation and the final answer. EU Data Zone quota was unavailable.
    Follow the
    [Azure resource inventory](infra/azure/README.md).

Add meaningful component tests alongside each step; step 8 brings those components
together for end-to-end validation.

## Containerisation

Status: implemented and validated locally on Linux ARM64. Depends on initial runtime.

1. **Package the application** — Reuse the synthetic composition with locked
   dependencies, pinned base images and non-root execution.
2. **Configure container startup** — Supply an explicit bind address, required
   demo token, readiness check and direct process execution for shutdown signals.
3. **Validate the container** — Exercise the offline workflow, token/input checks,
   two HTTP turns and graceful shutdown with a read-only filesystem.

Evidence: the [Docker verifier](scripts/verify_container.py) passed;
`uv run python scripts/verify.py` passed all 244 Python tests and static checks.
Other architectures and Azure execution remain unvalidated. See the
[container guide](src/bankeriq_agent_platform/demo/README.md#container-validation).

## PostgreSQL journal and job records

Status: implemented and validated locally on PostgreSQL 17.11. Depends on containerisation.

1. **Map existing storage operations** — Review `StateStore`, run transitions and
   `open_runtime()` before adding focused transactional queue methods. Define
   versioned conversation/event, checkpoint, job, result and approval records;
   map each transition to the architecture's atomic-write table. Keep clients,
   credentials and wire parsing outside core logic. Include tenant/conversation
   scope in related records; the initial adapter routes to one writable primary.
2. **Admit durable work atomically** — Append the actual permitted message delta,
   assign its conversation sequence, bind a stable tenant/request ID to its input
   fingerprint and create work in one transaction. Resolve unknown commits by ID.
   Keep one active logical run; later accepted turns queue in order and stay outside
   the current run's input context. Separate admission sequence from execution
   revision so accepting another message does not invalidate an active model call.
3. **Checkpoint bounded deltas** — Persist model/tool budgets, committed outcomes,
   delivery metadata and next work together. Keep confidential content out of
   shared records. Store large history in bounded pages; avoid rewriting the entire
   conversation on each message.
4. **Add schemas and indexes** — Version migrations and checkpoint formats. Use
   unique operation/event IDs, tenant-scoped lookups and partial indexes for due
   jobs/expired leases. Start with `READ COMMITTED`, explicit aggregate locks,
   consistent lock order and conditional revision checks. Define retention,
   deletion and database recovery generations before cleanup.
5. **Validate persistence** — Use a real local PostgreSQL container for rollback,
   restart, duplicate admission, stale revision, queued-turn ordering and independent
   process tests. Inject failure between writes in each implemented transaction
   and after commit before acknowledgement. Resolve retries by ID on the primary;
   check cross-tenant denial and output isolation.

Completed: accepted input and queued work survive a process restart together.
Job execution remains synthetic until the durable worker group. The
[storage guide](src/bankeriq_agent_platform/storage/README.md) maps implemented
transactions, bounds, retention prerequisites, generation fences and limitations.
Versioned migrations and the adapter live under `src/bankeriq_agent_platform/storage/`;
`open_runtime()` accepts the adapter through its existing store dependency.
The default executable remains in-memory.

Evidence: all 257 Python tests and static checks passed with PostgreSQL enabled,
including 13 PostgreSQL integration tests covering write-boundary rollback,
independent process admission/recovery, lost commit acknowledgements, duplicate
and conflicting IDs, stale revisions, ordered context, approvals, tombstones,
recovery generations and restricted-output isolation. Run the reproducible Docker
commands in the storage guide. Automatic recovery of running execution, worker
ownership, projection and production HA remain later groups.

## Shared system prompts

Status: implemented and locally validated. Depends on PostgreSQL journal and job records.

1. **Separate instructions from client lifetime** — Add explicit resolved
   instructions and a prompt manifest to the provider-neutral model request. Keep
   the existing synthetic startup prompt through a fixed resolver. Adapters encode
   request-scoped instructions without mutating shared model clients; history keeps
   its existing user/assistant/tool roles.
2. **Store and publish global versions** — Add immutable prompt bodies/hashes,
   compatibility/validation metadata and an application/environment active binding.
   Separate admin publication from chat/tool access. Validate drafts and activate
   with an expected binding revision; rollback creates a new activation record.
3. **Resolve at each new model step** — Read the binding during step preparation
   and persist the manifest before inference. All sessions inherit the current
   global version; in-flight steps and retries retain their resolved version.
   New steps after tool/approval waits pick up updates. Keep compatible runtime
   instructions available for recovery; unavailable versions block explicitly.
4. **Compose and cache safely** — Combine runtime and global guidance in fixed
   order, including parallel/repeated-tool guidance. Cache immutable bodies by
   version; the active binding remains a PostgreSQL read. Include the manifest in
   composed-context keys. Add Redis body caching in the Redis group; local immutable
   caching suffices here. Business overlays remain deferred.
5. **Validate propagation and recovery** — Test existing/new/idle sessions, two
   workers, publication during inference, resumption after tool waits, old-step
   retries, concurrent publishers, rollback and lost invalidation signals. Verify
   unauthorised publication is denied, client instructions cannot race between
   sessions, and prompt text never changes trusted tool permissions.

Completed: one activation affects every newly resolved model step without
restarting sessions. Prepared steps retain their exact manifest and recoverable
instructions. The separate management API supports immutable publication,
revision-checked activation/rollback and withdrawal. The
[prompt guide](src/agent_runtime/prompts/README.md) documents composition,
authorization, retention, reproduction commands and validation limits.

Evidence: `uv run python scripts/verify.py` passed all 275 tests and static checks
with local PostgreSQL 17.11 enabled. Fifteen shared-prompt PostgreSQL scenarios
cover propagation, prepared-step recovery, concurrent activation, rollback and
lost commit acknowledgements. Local HTTP tests
verify concurrent request-scoped instructions through one Foundry client. Tool
permissions and restricted-output boundaries remain enforced by application code.
Live model behavior, production admin authentication and Redis remain later work.

JSON authoring follow-up completed on 3 November 2026: [prompt definitions](prompts/README.md)
now separate runtime/system roots, security rules, guidelines and synthetic demo
text into versioned JSON compositions. Catalogs pin exact component versions and
file hashes. Migration 005 preserves publication source provenance. The pre-JSON
runtime-v1 compatibility files and composer have subsequently been removed. Application startup
performs file parsing;
core receives immutable prompt values. Offline authoring commands validate, render
and register new versions. Wheels and containers package the assets.

Follow-up evidence: all 292 tests and static checks passed with local PostgreSQL
17.11 enabled. The rebuilt synthetic container passed offline workflow, token,
readiness, non-root HTTP, two-turn and shutdown checks; packaged prompt loading
also passed without repository files. Live-model adherence to the new wording
has not been evaluated.

## Redis sessions and asynchronous history

Status: planned. Depends on PostgreSQL records and shared system prompts.

1. **Serve active views** — Hydrate recent context from a PostgreSQL snapshot plus
   journal tail, then serve API reads from tenant/conversation Redis keys. Page old
   history. Track schema, generation and durable cursor; reject stale refills and
   delayed updates. Test reads that require the caller's last accepted cursor.
2. **Separate pending and accepted state** — Allow immediate provisional UI state
   in Redis. Return durable acceptance only after the PostgreSQL admission commit;
   model/tool execution cannot start from Redis-only input. Reconcile pending
   display state by request ID after retries and reconnects. Record projection work
   with each durable transition; a failed cache refresh cannot undo acceptance.
3. **Project history asynchronously** — Record dirty cursors in PostgreSQL and
   materialise bounded history pages/snapshots in batches. Starting triggers are
   60 seconds, 100 events or 1 MiB; these require measurement. Persist cursor advance
   atomically and preserve events that arrive during projection.
4. **Handle idle cleanup** — Propose a 15-minute idle snapshot and 30-minute sliding
   Redis TTL. Discover dirty/idle work from PostgreSQL even when Redis is gone.
   Explicit cleanup compares the observed generation/cursor before deleting keys.
   Retain journal records until durable materialisation and all recovery/retention
   references permit deletion.
5. **Bound context and memory** — Keep only recent context and bounded progress
   streams in Redis. Preserve tool-call/result pairs and fail explicitly when the
   model context budget cannot be met. Semantic compaction remains follow-up work.
6. **Validate loss and races** — Delete Redis before idle, during a tool wait and
   after admission but before refresh. Recover accepted history; test concurrent
   hydration, stale cache writes, busy conversations that never idle, deletion
   tombstones and cleanup racing with a new turn. Crash after cache update before
   projection acknowledgement; retries must preserve newer cursors. Reject cache
   records from an earlier database recovery generation.

Completion requires Redis to be disposable and large conversations to avoid
synchronous full-history writes. Persisting small durable deltas remains required.
The projection loop can run explicitly in tests until durable workers exist.

## Durable PostgreSQL workers

Status: planned. Depends on PostgreSQL records and Redis recovery semantics.

1. **Claim work safely** — Use short `SKIP LOCKED` transactions, database-time
   leases, heartbeats and monotonic ownership tokens. Never hold a DB lock during
   external I/O. Reject expired/stale settlement and renewal.
2. **Wake and recover** — Add LISTEN/NOTIFY with ID-only payloads and indexed poll
   fallback. A dedicated listener connection handles session semantics. Sweep
   expired jobs and find due retries without relying on Redis or notifications.
3. **Settle atomically** — Commit unique output, completed job, checkpoint change
   and next job in one transaction. Stable IDs and input fingerprints distinguish
   matching retries from conflicting reuse. Keep cache projection repair durable.
   Claims, renewals, settlement and unknown-commit lookups always use the primary;
   database retry loops never repeat tool/model I/O.
4. **Persist retry and wait state** — Save backoff deadlines, attempt/run budgets,
   approval expiry and cancellation. Recheck access after waits. Unsafe operations
   with unknown outcomes enter reconciliation; replay requires tool idempotency
   or saved/current replay permission.
5. **Bound and drain workers** — Limit per-queue and per-tenant work, reserve API
   connection capacity and stop claiming before shutdown. Preserve compatible
   worker versions for persisted work; maintenance initially shares bounded tasks
   in the worker deployment.
6. **Validate ownership** — Run two independent worker processes with synthetic
   handlers. Kill the claimant, drop notifications, delay completions beyond lease
   expiry, race renewals and settlements, and verify duplicate-safe logical results.

Completion requires one accepted job to recover across worker loss and duplicate
notifications. Model and tool orchestration remain synthetic until the next group.

## Asynchronous coordinator and tool queues

Status: planned. Depends on durable workers. This is part of the infrastructure
milestone; parallel tool execution has moved out of follow-up scope.

1. **Extract resumable model steps** — Adapt the existing coordinator loop to commit
   one model-step outcome and its next work. Retain the in-memory synchronous
   profile as a reference. Add an async provider dependency at the model boundary;
   keep SDKs, pools and worker scheduling outside core runtime logic. Resolve and
   pin the prompt manifest when preparing each new step; retries reuse it.
2. **Queue tool calls** — Commit validated model output, pinned tool intent and
   child jobs together before dispatch. Parallel execution is the bounded default
   for every call in the reply, including repeated calls to the same tool. Explicit
   trusted resource-conflict rules can require ordering; a shared tool name alone
   cannot. Document batching in model instructions and validate provider support.
   Dependent calls belong in later model steps after their inputs arrive. Preserve
   distinct call IDs even for identical arguments; retries reuse their original
   identity. Tool workers recheck access and approvals for each operation.
3. **Join results once** — Record each outcome and job settlement atomically with
   its pending group. Serialize the group update and uniquely key the resume job.
   Preserve tool-result order in model context while allowing out-of-order UI
   progress. Adapt the existing continuation handler rather than creating a second
   recovery owner.
4. **Keep API and workers responsive** — Return an accepted run ID; release the
   coordinator slot during tool/approval waits. Use bounded async I/O, isolate
   blocking handlers and CPU-heavy tools, and preserve model/tool budgets across
   resumption. No thread remains allocated to an idle chat.
5. **Deliver progress and final results** — Add bounded SSE/progress events through
   Redis with run/attempt generations. Reconnect uses a durable cursor and rebuilds
   after stream loss. Persist final answers before saved acknowledgement. Forward
   synthetic tool-owned report links separately from model receipts.
6. **Validate async behaviour** — A slow tool must not block another conversation.
   Test simultaneous tool completion, partial failure, cancellation, revoked access,
   client disconnect, lost token stream and interrupted model calls. Check that
   confidential payload markers never enter model context, journal or traces.
   Use barriers to prove overlap for multiple calls to the same tool. Include
   distinct IDs with identical arguments, duplicate delivery of one ID, resource
   serialization, per-call budgets and out-of-order results with one continuation.

Completion requires a real local PostgreSQL/Redis conversation to fan out synthetic
jobs, release its coordinator, resume once and survive a worker restart. Streaming
transport tests do not require implementing the deferred browser application.

## Recovery and horizontal scaling validation

Status: planned. Depends on asynchronous coordinator and tools.

1. **Exercise crash boundaries** — Kill processes after admission, claim, model
   response, tool effect, result commit, join and final persistence. Cover ambiguous
   side effects and unknown commit outcomes without automatic unsafe replay.
   Verify each atomic-write group leaves either its complete transition or no
   transition after rollback, including cancellation racing with settlement.
2. **Test migrations and retention** — Run mixed compatible worker versions, reject
   unsupported checkpoints, preserve old state on migration failure, and verify
   retention cannot remove deduplication/recovery evidence early. Deletion cannot
   be undone by a delayed projection or worker.
3. **Measure concurrency** — Use multiple API, coordinator and tool processes.
   Preserve one active logical run per conversation, fairness between tenants and
   bounded model requests. Measure queue age, commit latency, warm/cold reads,
   first-token latency, projection lag and DB resource usage under stated load.
4. **Tune budgets and scale signals** — Set lease/heartbeat, batch, idle and replica
   limits from evidence. Scale on eligible backlog, excluding sleeping retries and
   approval waits. Verify backpressure, bounded caches and connection-pool limits.
5. **Validate recovery operations** — Reconstruct after full Redis loss, exercise
   PostgreSQL unavailability and drain workers. On primary reconnection, resolve
   outstanding IDs before retrying. Restore a database backup with dispatch paused;
   fence old workers, advance the recovery generation, rebuild Redis and reconcile
   effects and acknowledged requests beyond the restored point before resuming.
6. **Validate read routing** — Check the initial adapter uses the primary for all
   database operations. Claims, deduplication, prompt activation reads and
   cursor-required recovery retain this route when optional replica reads arrive.
   Require a local controlled-lag test before enabling replica history/reporting.
   Azure HA failover is a separate hosted gate.

Completion requires executed failure and load evidence with explicit limits.
Horizontal worker capacity remains bounded by PostgreSQL and model/tool quotas.

## Azure runtime deployment

Status: planned. Depends on recovery and scaling validation. Deployment still
requires an explicit request.

1. **Settle hosting compatibility** — Container Apps is proposed for API/coordinator/
   tool pools, with Foundry inference. Record the decision against the earlier
   Foundry Hosted Agents target. If Foundry hosting remains required, validate its
   polling, private connectivity, async lifecycle and PostgreSQL recovery ownership.
   Keep the existing adapter/tests and distinguish each host's validation evidence.
2. **Prepare provider-managed infrastructure** — Define PostgreSQL, Redis, chosen
   compute, Registry, identities, private networking/DNS and approved monitoring in
   Terraform. Recheck EU availability, model version/quota and Python/container
   compatibility. Regional Sweden is approved for synthetic tests; old soft-deleted
   names may remain reserved.
3. **Integrate authentication and scaling** — Implement Entra user/input admission
   and scoped worker identities. Validate Container Apps' PostgreSQL scaler and
   authentication; use fixed bounded replicas if unsupported. Select appropriate
   pooling, production HA, backups and log retention separately from pilot sizing.
   Set recovery-point/time targets and writer/read connection routes explicitly;
   distinguish managed HA standby from optional asynchronous read replicas.
4. **Publish and deploy when requested** — Publish immutable images, review the
   Terraform plan, then apply within deployment authorisation. Follow the
   [Azure inventory](infra/azure/README.md); shared Terraform state is separately
   provider-managed infrastructure.
5. **Validate the hosted lifecycle** — Test warm/cold starts, multiple replicas,
   private access, token refresh, scale-in/drain, Redis loss, PostgreSQL failover/
   restore and model throttling. Use synthetic tools and authenticated report-link
   fixtures; tool-specific Blob storage remains separate work.

Completion requires executed evidence on the chosen host and stated load profile.
Foundry inference alone does not establish Foundry Hosted Agents execution.

## Follow-up runtime work

Status: planned after the infrastructure groups.

1. **Semantic context compaction** — Summarise older permitted history with versioned
   summaries, preserving instructions, tool pairs, provenance and output policy.
2. **Richer streaming and scheduling** — Extend the initial progress/reconnect path
   and tune fairness or priorities only when measured workloads require it.
3. **Database growth** — Partition/archive large event and job tables; consider
   larger primary capacity before adding write shards. Add read replicas only for
   measured lag-tolerant demand; they do not increase write capacity.
4. **Tenant sharding, conditional** — Implement trusted tenant routing only after
   measured write bottlenecks justify it. Co-locate all tenant conversation/queue/
   deduplication records. Test fenced shard moves and prompt resolution from the
   authoritative configuration writer; splitting one tenant needs a separate design.
5. **Broker transport, conditional** — If measured transport needs justify it, add
   transactional outbox/inbox adapters. Test publish-before-ack crashes, duplicate
   and out-of-order delivery, schema compatibility and replay retention. Preserve
   local atomic transitions and explicit reconciliation across databases. Broker
   infrastructure and cross-database transactions are outside the initial build.
6. **Business prompt overlays** — Add a trusted user-class or usage-profile binding
   over the global prompt with versioned composition. Define assignment and change
   semantics before supporting multiple overlays; global updates still propagate.

## Scope boundary

Real tool applications, durable workers, persistent storage, enterprise identity
integration and the browser app remain outside the completed initial core scope.
Tools own confidential Blob reports, authenticated download endpoints and report
lifecycle. The runtime forwards approved delivery links for the user to open and
verify; it has no report-read access. Tool storage is outside this build sequence.
Permission checks and output enforcement are real runtime logic; their external
providers and execution dependencies are mocked. The self-hosted specialist is
deferred.

## Durability design references

The planned persistence, recovery and handoff groups incorporate source review
of Pi's durable package at commit `95485d32f19d5ab9dca81bf2d7299661347d3238`
on 2 November 2026. Pi's tests were inspected, not executed. These are proposed
BankerIQ adaptations; only groups marked implemented have execution evidence.

- **Intent and replay policy**
  Planned adaptation and source: Persist per-call intent and require saved/current policy to permit
  recovery. [Pi tool recovery][line-source-1].

- **Atomic settlement**
  Planned adaptation and source: Commit conversation results, deduplication and the next queued job
  atomically in PostgreSQL. [Pi settlement][line-source-2].

- **Persistent retry state**
  Planned adaptation and source: Preserve deadlines and restart counts; keep orderly shutdown
  distinct. [Pi task states][line-source-3].

- **Durable identity**
  Planned adaptation and source: Deduplicate admission and bind IDs to matching intent. Pi's
  top-level request check is weaker than its nested-call check; use argument/version matching for
  BankerIQ. [Pi request admission][line-source-4], [nested-call identity][line-source-5].

- **Versioned checkpoints**
  Planned adaptation and source: Reject incompatible persisted work initially; introduce explicit
  migrations when needed. [Pi definitions][line-source-6], [compatibility tests][line-source-7].


Pi assumes [one process owns storage][line-source-8].
Its startup recovery resets running tasks to pending. BankerIQ needs cross-process
leases, ownership tokens and revision checks before reclaiming jobs. Existing
`StateStore`, coordinator and continuation boundaries remain the starting point;
adopting Pi's single-process scheduler is outside this plan.

[PostgreSQL queue locking](https://www.postgresql.org/docs/current/sql-select.html)
and [LISTEN/NOTIFY semantics](https://www.postgresql.org/docs/current/sql-notify.html)
inform worker claims and wake-ups. The
[Redis persistence documentation](https://learn.microsoft.com/en-us/azure/redis/how-to-persistence)
explains why idle-only flushing cannot guarantee recovery of accepted turns.
[Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app)
and the [KEDA PostgreSQL scaler](https://keda.sh/docs/2.21/scalers/postgresql/)
inform deployment validation; availability in the actual host remains a gate.

[line-source-1]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L181-L226
[line-source-2]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L828-L872
[line-source-3]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/types.ts#L657-L746
[line-source-4]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/submissions.ts#L211-L240
[line-source-5]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L898-L946
[line-source-6]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/types.ts#L364-L385
[line-source-7]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/test/harness-tasks-recovery.test.ts#L448-L525
[line-source-8]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/README.md#L854-L883
