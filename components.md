# Logical components

Design updated 2 November 2026. This catalogue describes target responsibilities;
the selected PostgreSQL/Redis and asynchronous worker integrations are still planned.
Current runtime implementation and test coverage are tracked in the
[build order](build-order.md). Several components can
share an application. Selected Azure services and state ownership are recorded in
[ARCHITECTURE.md](ARCHITECTURE.md#selected-state-and-execution-services).

The initial platform uses Microsoft Foundry for model inference and a dedicated
browser chat interface. Container Apps is proposed for the API and worker pools;
Foundry Hosted Agents compatibility remains a separate deployment decision.
The self-hosted specialist is deferred. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the overall design and security boundaries.

## Browser chat client

Displays conversations, streamed answers, approval requests, job progress and
authenticated report links. Sends user messages and explicit approval decisions
to the application backend. It does not decide permissions or hold business-tool
credentials. Restricted report content stays separate from model conversation
history, even when displayed in the same application.

## Application backend

Owns the interface between the browser and runtime. Binds authenticated users to
conversations, accepts messages, starts runs and delivers response events. Checks
access before reading or resuming conversations and forwards tool-owned report
links directly to the user. The tool's endpoint handles download authentication
and resource access. Raw input handling remains unresolved: owning the browser client does
not establish whether submitted text or attachments may reach the external model.

Serves active views from Redis, acknowledges acceptance after the PostgreSQL input
and queue transaction, and streams progress independently from execution. Client
reconnects carry durable cursors; pending display state is labelled explicitly.

## Identity, permissions and approvals

Provides verified user and tenant context and evaluates enterprise permissions
for tools, operations and resources. Defines which actions require approval and
records approval against the exact operation and parameters, with expiry and
replay protection. The model can propose an action but cannot grant access or
approve it. Access is rechecked at execution and delivery, including after waits.

## Coordinator runtime

Owns the agent loop: prepare permitted context, call the external model, discover
relevant authorised tools, dispatch calls and continue from visible results or
restricted-job receipts. Bounds tool calls and execution, handles cancellation
requests and publishes progress. It cannot reason over restricted content it does
not receive. Tool configuration is scoped to each run rather than mutated globally.

The coordinator consumes provider-neutral model requests and replies. Model and
hosting adapters live under `providers/`; application startup selects them
independently through trusted configuration. SDK credentials and API response
formats remain within those adapters. Foundry is the initial implementation.

The target executes one resumable model step at a time. A committed tool batch
creates durable jobs and releases the coordinator slot. Calls execute in parallel
by default, including repeated calls to the same tool, within configured capacity
and resource-conflict limits. Trusted model instructions explain batching and
requesting dependent calls after their inputs arrive. Distinct call IDs preserve
each result and retry identity; one continuation resumes the model after the group
settles. The current implementation still runs a synchronous loop.

## Shared prompts and context assembly

An application-owned resolver supplies immutable instructions for each model step.
PostgreSQL stores published global versions and the active binding for the
application/environment; all sessions inherit that binding. Redis or worker memory
caches immutable bodies by version. The active binding is checked during durable
step preparation, so missed cache invalidations cannot preserve stale instructions.

Authorised admin operations publish, activate and roll back versions with audit
records and optimistic concurrency. Newly resolved steps use the latest version;
in-flight steps and retries retain their recorded manifest. The context builder
combines runtime instructions, the global prompt, permitted history and tool
definitions, keeping prompts outside chat history. Existing startup instructions
are fixed for the lifetime of the current Foundry client; live resolution is planned.

Business/user-class overlays are deferred. Their future selection comes from
trusted application context and retains the global layer. Prompt changes cannot
grant permissions, remove output enforcement or change tool concurrency limits.

## Tool registry and discovery

Stores versioned tool definitions, argument schemas, required permissions,
approval requirements, execution targets and output policies. Returns definitions
filtered by the user's current permissions. The coordinator can discover additional
tools as a request develops; discovery exposes existing permissions and never
expands them. Tool descriptions must themselves be safe for the external model.

## Tool gateway and output enforcement

Validates every requested call against its schema and current tool, operation,
resource and approval policies using trusted runtime identity. Dispatches only
authorised work and enforces the registered output policy on every return path:

- **Visible:** validated data permitted in the external model's context.
- **Restricted:** fixed status fields and opaque references only; payloads remain
  internal and reports are delivered separately.

Failures, streaming events and retries follow the same policy. Worker-generated
text cannot become an unrestricted status message. Other tools cannot retrieve a
restricted artifact into visible context to bypass its policy.

## Tool implementations

Perform bounded business operations, retrieval and computation. They enforce
resource access, use scoped dependencies and return results through output
enforcement. Initially, restricted workloads use deterministic applications;
tasks requiring an internal model remain unsupported until the specialist exists.
They must not fall back to sending restricted data to the external coordinator.

## Durable job execution

PostgreSQL jobs carry durable model-step, tool and continuation work. Workers claim
due rows in short transactions using leases and ownership tokens. They release
database locks before external I/O, renew ownership while executing, and settle
results and next work atomically. Notifications reduce wake-up latency; polling
recovers missed notifications and due retries.

The application owns retry deadlines, attempt budgets, approval waits, cancellation,
dead jobs, version compatibility and ambiguous-effect reconciliation. Duplicate
delivery must not duplicate logical results. Repeating an external effect requires
tool idempotency or explicit replay permission. Independent worker pools scale by
eligible backlog and tool/model limits. SDKs, timers, credentials and scheduling
remain outside core logic; raw restricted payloads stay inside tool boundaries.

## Conversation persistence

PostgreSQL owns conversation identity, append-only permitted events, versioned
checkpoints, approvals and delivery metadata. Admission commits the input delta,
request fingerprint and work item together. Completion commits the result and
continuation together. Preserve revisions and one active logical run per
conversation; later accepted turns wait in sequence.

History pages and bounded context snapshots are materialised asynchronously from
the journal, on size/time thresholds and idle. Redis loss can reconstruct accepted
messages even before that materialisation. The existing `StateStore` remains the
starting point; introduce focused transactional queue operations as needed.

The storage adapter owns the [transaction boundaries](ARCHITECTURE.md#transaction-and-replication-boundaries),
uniqueness constraints, lock order and primary routing. Commit projection work
alongside state changes. Resolve unknown commits by stable IDs on the primary.
Optional replicas serve explicitly lag-tolerant history/reporting; job ownership,
active prompt resolution and current access/deletion checks use the primary.
Future tenant sharding keeps related records together. A separate broker would use
outbox/inbox delivery with duplicate handling; both integrations remain deferred.

## Ephemeral session state

Redis is the main active UI read path through the API. It holds recent history,
bounded context, pending display state and transient progress. Hydrate from a
PostgreSQL snapshot plus journal tail. Distinguish provisional messages/tokens
from accepted durable records, and reject stale cache writes by cursor/generation.

A configurable idle TTL removes cached state. PostgreSQL dirty records drive
snapshot/cleanup work even if Redis has disappeared. Cleanup cannot delete a
newer session generation. Reconnects load the same conversation from durable
records; Redis grants no permissions and owns no execution leases.

PostgreSQL and Redis update independently. Failed cache updates leave durable
projection work available for repair. A database restore advances the recovery
generation and rebuilds cached views before they can serve recovered sessions.

## Projection and maintenance

Materialises bounded history pages, refreshes Redis from committed events and
tracks the last projected cursor. Retried projection is idempotent. Scans for
expired execution leases and due work, performs bounded retention cleanup and
preserves deletion tombstones. Initially shares a bounded worker role; move it
to separate compute only when workload evidence justifies that change.

## Tool-owned reports and authenticated delivery

The report-producing tool writes private Blob artifacts and owns access metadata,
retention, deletion and authenticated download endpoints. It returns an opaque
link that the runtime forwards in structured delivery metadata. Report contents
and sensitive filenames stay outside external-model history and traces.

The coordinator identity has no report-read access. The user authenticates to the
tool endpoint to open and verify the report; possession of its URL grants no
access. Runtime work covers output enforcement and link forwarding. Building
the tool's storage and download service is outside the runtime infrastructure phase.

## Audit and operational visibility

Records verified actors, operations, policy decisions and execution outcomes using
correlation IDs. Supports investigation of failures and performance without
copying restricted payloads into logs, traces or model telemetry. Audit records
and operational metrics have their own access and retention controls.

## Deferred: self-hosted specialist

A smaller internal model will perform bounded analysis where deterministic tools
are insufficient. It will use authorised internal data and computation tools,
write protected artifacts and expose only approved receipts to the coordinator.
Model quality, hosting cost and permitted network access require validation before
this component is introduced.

## End-to-end interaction

1. The backend authenticates a browser request and checks conversation access.
2. The runtime receives permitted input and discovers tools within the user's
   permissions.
3. The gateway validates a proposed call and obtains any policy-required approval.
4. The runtime commits tool intent and jobs; workers execute the batch concurrently
   within trusted limits, including repeated calls to one tool, and atomically
   schedule the next model step after settlement.
5. Visible results return to the coordinator. Restricted results stay internal,
   with status returned to the coordinator and protected reports sent to the user.
