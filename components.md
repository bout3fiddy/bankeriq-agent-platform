# Logical components

Proposed design, 28 October 2026. This catalogue describes target responsibilities.
Current runtime implementation and test coverage are tracked in the
[runtime build order](src/agent_runtime/build-order.md). Several components can
share an application. Cloud services and deployment choices will be documented separately
in `services.md`; that document is not created yet.

The initial platform uses Microsoft Foundry for the coordinator and a dedicated
browser chat interface. The self-hosted specialist is deferred. See
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
access before reading or resuming conversations and routes report delivery directly
to the user. Raw input handling remains unresolved: owning the browser client does
not establish whether submitted text or attachments may reach the external model.

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

Executes work that must survive request or process lifetimes. Claims persisted
jobs, records progress, manages timeouts and retries, responds to cancellation and
recovers interrupted work. Uses leases or equivalent ownership controls to avoid
competing workers and idempotency mechanisms to limit duplicate side effects.
Ambiguous outcomes require reconciliation rather than blind retries.

This responsibility can initially live within the runtime; it does not imply a
separate service. The coordinator initiates and monitors work, while a worker or
recovery loop executes it independently of an active model call. Persisting job
records alone does not provide durable execution or exactly-once side effects.

## Conversation and job state

Persists conversation ownership, permitted history, run status, pending approvals,
job transitions, execution checkpoints and artifact references. Supports atomic
state changes, worker ownership and recovery without relying on process memory.
Keeps restricted payloads out of externally visible conversation history and job
metadata. Storage supplies state; execution logic decides how work progresses.

## Artifact storage and authenticated delivery

Stores reports and their ownership/access metadata privately. Returns protected
report routes directly through the application backend. Every download requires
an authenticated session and a fresh resource-access check; possession of a link
is insufficient. Manages retention and deletion without exposing report contents
or sensitive filenames to the coordinator.

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
4. A tool executes immediately or through durable job execution; state records
   support progress tracking and recovery.
5. Visible results return to the coordinator. Restricted results stay internal,
   with status returned to the coordinator and protected reports sent to the user.
