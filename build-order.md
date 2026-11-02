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
numbering. Durable PostgreSQL workers are next.

The selected stack is PostgreSQL plus ephemeral Redis. PostgreSQL contains durable
conversation deltas, history snapshots and the message queue. Redis serves active
UI views through the API; large history writes happen asynchronously and on idle.
Accepted work always has a recoverable PostgreSQL input record. See the
[full system design](ARCHITECTURE.md#selected-state-and-execution-services).
The [transaction and replication boundaries](ARCHITECTURE.md#transaction-and-replication-boundaries)
define atomic writes, primary-only reads and recovery requirements for the groups
below. The PostgreSQL journal adapter and its local failure tests are implemented;
later worker and deployment guarantees remain pending.
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
    `uv run python -m tests.validation serve` assembles mock tools, in-memory storage,
    continuation fakes and explicit test authentication/input policy through
    `open_runtime()`. `uv run python -m tests.validation validate` checks the synthetic
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
   validation token, readiness check and direct process execution for shutdown signals.
3. **Validate the container** — Exercise the offline workflow, token/input checks,
   two HTTP turns and graceful shutdown with a read-only filesystem.

Evidence: the [Docker verifier](scripts/verify_container.py) passed;
`uv run python scripts/verify.py` passed all 244 Python tests and static checks.
Other architectures and Azure execution remain unvalidated. See the
[container guide](tests/validation/README.md#container-validation).

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
ownership and production HA remain later groups. Projection was added in the Redis group.

## Shared system prompts

Status: implemented and locally validated. Depends on PostgreSQL journal and job records.

1. **Separate instructions from client lifetime** — Add explicit resolved
   instructions and a prompt manifest to the provider-neutral model request. Use the
   runtime/system catalog for synthetic validation too. Adapters encode
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
Live model behavior and production admin authentication remain later work.
Redis immutable-body caching was subsequently added in the session group.

JSON authoring follow-up completed on 3 November 2026: [prompt definitions](prompts/README.md)
now separate runtime/system roots, security rules and guidelines into versioned
JSON compositions. Synthetic validation fixtures subsequently moved under tests/validation/.
Catalogs pin exact component versions and
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

Status: implemented and locally validated. Depends on PostgreSQL records and shared system prompts.

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
   atomically and preserve events that arrive during projection. Version the JSON
   snapshot shape and rebuild from its committed cursor plus subsequent deltas;
   verify this produces the same permitted view as replaying the retained journal.
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
The projection loop runs explicitly until durable workers exist. The
[session guide](src/bankeriq_agent_platform/sessions/README.md) documents API wiring,
limits, projection/cleanup loops, retention and reproducible container commands.
Immutable prompt bodies also have an optional Redis cache; primary checks remain required.

Implemented: bounded snapshots/pages, durable dirty discovery, cursor/generation fences,
provisional reconciliation, recent complete-turn context and explicit context overflow.
Local synthetic tests cover Redis loss, restart, concurrent hydration, stale updates,
projection rollback/unknown commits, tenant/output isolation, busy conversations,
idle races, fair projection scans and immutable prompt cache corruption/withdrawal.
No automatic scheduler, worker execution ownership or journal pruning is added.

Executed on 3 November 2026: the full verification gate passed 324 tests and static
checks with PostgreSQL 17.11 and Redis 7.4.12 enabled, including 21 session scenarios.
The rebuilt synthetic container passed its offline/HTTP/shutdown checks; wheel
inspection confirmed packaged Lua and migrations. The session guide records local
read/projection samples and limits. No cloud or live-model calls were made.

## Durable PostgreSQL workers

Status: planned. Depends on PostgreSQL records and Redis recovery semantics.

1. **Define serializable recovery state** — Extend the existing storage codec and
   versioned checkpoint records with explicit task kind/version, phase, bounded
   inputs or references, operation identity, waits and retry state. Encode strict
   JSON at the adapter boundary; reject unsupported values and non-finite numbers
   before writes. Persist permitted data only; keep callables, credentials, clients
   and process-local handles in worker composition. Resolve a registered handler
   from the saved kind/version and resume its phase with fresh dependencies.
2. **Claim work safely** — Use short `SKIP LOCKED` transactions, database-time
   leases, heartbeats and monotonic ownership tokens. Never hold a DB lock during
   external I/O. Reject expired/stale settlement and renewal.
3. **Wake and recover** — Add LISTEN/NOTIFY with ID-only payloads and indexed poll
   fallback. A dedicated listener connection handles session semantics. Sweep
   expired jobs and find due retries without relying on Redis or notifications.
4. **Settle atomically** — Commit unique output, completed job, checkpoint change
   and next job in one transaction. Stable IDs and input fingerprints distinguish
   matching retries from conflicting reuse. Keep cache projection repair durable.
   Claims, renewals, settlement and unknown-commit lookups always use the primary;
   database retry loops never repeat tool/model I/O. Order transitions for each
   affected conversation/run/group through the existing row-lock order and revision
   checks across processes. Publish accepted/saved state after commit; independent
   conversations and external handlers retain bounded concurrency.
5. **Persist retry and wait state** — Save backoff deadlines, attempt/run budgets,
   approval expiry and cancellation. Recheck access after waits. Unsafe operations
   with unknown outcomes enter reconciliation; replay requires tool idempotency
   or permission from both the saved and current replay policies. Save final
   validated arguments, version and operation identity before dispatch; reuse that
   identity for any provider idempotency lookup. A saved intent alone cannot prove
   whether an external effect happened.
6. **Bound and drain workers** — Limit per-queue and per-tenant work, reserve API
   connection capacity and stop claiming before shutdown. Preserve compatible
   worker versions for persisted work; maintenance initially shares bounded tasks
   in the worker deployment.
7. **Validate ownership and reconstruction** — Run two independent worker processes with synthetic
   handlers. Kill the claimant, drop notifications, delay completions beyond lease
   expiry, race renewals and settlements, and verify duplicate-safe logical results.
   Round-trip each supported checkpoint phase into a fresh process; prove invalid
   encoding leaves no partial writes and missing/incompatible handlers cannot run.
   Race two settlements to prove one committed result and continuation.

For example, persist phase `charge` and operation ID `payment-123` before a
synthetic payment. If its effect succeeds but result persistence fails, recovery
uses the same ID to resolve or safely retry the payment, or enters reconciliation.
The saved phase determines recovery; a vanished Python stack provides no evidence.

Completion requires one accepted job to recover across worker loss and duplicate
notifications. Model and tool orchestration remain synthetic until the next group.

## Asynchronous coordinator and tool queues

Status: planned. Depends on durable workers. This is part of the infrastructure
milestone; parallel tool execution has moved out of follow-up scope.

1. **Extract resumable model steps** — Adapt the existing coordinator loop to commit
   one model-step outcome and its next work. Retain the in-memory synchronous
   profile as a reference. Add an async provider dependency at the model boundary;
   keep SDKs, pools and worker scheduling outside core runtime logic. Resolve and
   pin the prompt manifest when preparing each new step; retries reuse it. Use the
   worker group's versioned phase records to reconstruct each model/tool boundary;
   recovery invokes the saved phase and may repeat its unfinished external call.
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
   synthetic tool-owned report links separately from model receipts. Label partial
   tokens and tool progress as disposable; they cannot drive durable decisions.
   Admission, intent, checkpoints and final outcomes always require a PostgreSQL
   commit. Test that losing displayed progress preserves all accepted work.
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
   be undone by a delayed projection or worker. Include strict-JSON rejection,
   encode/decode round trips and restart from each phase. Exercise every saved/current
   replay-policy combination, including a policy revoked between attempts.
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

## Reviewed composite business tools

Status: planned application tooling after the infrastructure groups. Depends on
asynchronous coordinator/tool queues and recovery validation. No business tools or
code-mode executor are implemented by this plan section.

Expose focused, versioned business operations through the existing tool interfaces.
Application developers define and review their composition. The Foundry model
supplies schema-validated parameters through ordinary function calling; the runtime
dispatches the operation. Internal API clients and credentials stay behind tool
implementations. A composite can call several tools asynchronously and suspend
for durable jobs or approvals without holding an execution slot.

1. **Choose a bounded business workflow** — Start with a concrete operation such as
   `compare_loan_scenarios`, `check_document_completeness` or
   `generate_portfolio_report`. Specify inputs, permitted outputs, maximum fan-out,
   partial-failure behaviour and required approvals. Keep financial calculations
   and rounding in reviewed domain implementations. Reject unsupported options;
   do not accept arbitrary scripts or model-defined execution graphs.
2. **Register and bind the composite** — Use `ToolDefinition` for its schema,
   version, permissions, approvals and output policy, and the execution interfaces
   established by the async queue group. Pin the composition and child operation
   versions for recovery. Reuse the registry, gateway and worker lifecycle rather
   than introducing another scheduler or a general-purpose shell tool.
3. **Authorise every child operation** — Dispatch nested tool requests through the
   gateway with trusted tenant/user context. Recheck resource access and approvals
   at execution and after waits; access to the composite grants no additional
   child permissions. Bind approvals to exact operation/version/arguments and
   account for each child against run budgets and trusted concurrency limits.
4. **Persist asynchronous progress** — Save the composite phase, stable child-call
   IDs, validated arguments, dependencies and permitted outcomes. Fan out
   independent children within capacity and resource-conflict limits; start
   dependent children after their inputs commit. Reuse durable joins and one
   continuation, with cancellation and partial-failure rules. Retry unfinished
   effects only under saved/current replay policy and idempotency guarantees;
   ambiguous effects require reconciliation.
5. **Preserve output boundaries** — Feed only gateway-enforced child results into
   general orchestration. Restricted processing remains inside the authorised
   business implementation; expose fixed receipts and separate authenticated
   delivery metadata. Aggregation, exceptions, progress, journals and traces must
   not expose restricted payloads or turn an artifact reference into read access.
6. **Validate with synthetic workflows** — Test bounded fan-out, dependency order,
   partial failure, duplicate completion, revoked access during a wait, approval
   expiry, cancellation and restart between children. Verify completed effects
   are not repeated and one permitted parent result reaches the model. Check
   restricted markers across model requests, persisted state and telemetry.

Example: `compare_loan_scenarios` accepts an approved scenario reference and three
rate assumptions. Its reviewed implementation obtains permitted inputs, queues
three calculation children and joins their approved comparison rows. A restart
after two results reconstructs the pending third child using its original identity.
For `generate_portfolio_report`, confidential positions stay inside the report tool;
the coordinator receives a completion receipt and the user receives protected
delivery metadata. These are proposed workflows, initially exercised with fakes.

Completion requires one composite workflow to pass local asynchronous recovery,
permission and output-isolation tests with documented limits. Adding business
operations does not itself authorise production integrations or deployment.

### Conditional follow-up: model-written code mode

Defer code mode until actual read-only workflows demonstrate a need for variable
composition beyond ordinary tool calls and reviewed composites. An `execute_code`
tool would require a sandbox plus generated `tools.*` proxies that translate script
calls into requests through the existing tool interfaces. Those proxies do not
exist today. Naming a batch tool `execute_tools` does not add script execution.

Before enabling it, require host-enforced CPU/memory/deadline/output limits,
per-child authorisation and budgets, and durable suspension/replay through the same
job lifecycle. Only externally permitted data may enter model-written scripts or
their stored state; scripts can print anything they can read. Recheck access before
reusing stored results. Disable general shell/network access and direct model-call
helpers. Validate replay compatibility, permission changes, isolation and measured
workflow benefit before deciding whether to enable this optional capability.

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

Serialization follow-up reviewed on 3 November 2026 against the local Pi checkout
at commit `f9daca3480732d0042b1df3a6e27221f597a620d`. This adds planned requirements
to Redis history, durable workers, asynchronous execution and recovery validation;
completed storage milestones retain their existing evidence. Pi tests were read
only. No Pi dependency or Chord integration is proposed.

- **Serialized recovery state**
  Persist strict JSON phase records and reconstruct handlers from registered
  definitions. Keep executable objects and credentials outside storage. Extend
  [the existing codec](src/bankeriq_agent_platform/storage/codec.py) within its
  permitted variants and bounds. [Pi JSON ownership][pi-json-source] and
  [phase recovery][pi-phase-source] inform worker steps 1 and 7.

- **Serialized commits**
  Pi queues Session mutations through storage settlement and adoption. BankerIQ
  applies its existing PostgreSQL locks, revisions and ownership checks across
  workers; one process-local queue cannot establish that guarantee. See worker
  step 4 and [Pi's Session mutation boundary][pi-commit-source].

- **Incremental state and disposable progress**
  Pi stores document deltas with periodic bases and permits deferred persistence
  for model partials/tool progress. BankerIQ uses bounded journal snapshots and
  Redis progress, with accepted execution state committed to PostgreSQL. See Redis
  step 3, asynchronous execution step 5, [Pi checkpoints][pi-checkpoint-source]
  and [deferred persistence][pi-progress-source].

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

Validation harness cleanup: synthetic tools, model, test authentication, CLI and
transcript/scoring helpers now live under `tests/validation/`. The application
retains reusable `LocalServer` and `open_runtime()` wiring. The test-only system
prompt was removed; both mock and live-provider validation use the real BankerIQ
runtime/system definitions. The synthetic Docker image explicitly includes this
harness; the application wheel excludes it. Earlier live evidence used prescribed
instructions and does not validate the current prompt composition.

Executed on 3 November 2026: `uv run python scripts/verify.py` passed all 301
tests with local PostgreSQL 17.11 enabled, plus static checks. The rebuilt synthetic
container passed offline workflow, token, readiness, two-turn HTTP and shutdown
checks. An isolated wheel loaded the application catalog and reusable server with
no validation fixtures or synthetic CLI packaged. No live-model calls were made.

[pi-json-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1518-L1549
[pi-phase-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L2038-L2163
[pi-commit-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/src/session/session.ts#L155-L171
[pi-checkpoint-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1562-L1613
[pi-progress-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1834-L1852
