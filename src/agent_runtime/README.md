# Agent runtime workshop

Workshop and implementation workspace for the core agent runtime. Core data types,
tool registry, completed-output enforcement, synchronous tool gateway, in-memory
state storage, coordinator loop and mocked event continuation under `durable/` are implemented.
The Foundry model adapter implements inference calls with local HTTP tests;
live Azure validation remains pending.
Microsoft Foundry remains the initial coordinator target;
the self-hosted specialist is deferred.

## Scope

| Logical component | Responsibility |
| --- | --- |
| Coordinator runtime | Own the model/tool loop, select per-run tools and continue from permitted results. |
| Tool registry | Describe tools, schemas and policies; discover tools within the user's permissions. |
| Tool gateway and output enforcement | Authorise calls, dispatch execution and enforce visible or restricted output policies. |
| Tool implementations | Perform bounded operations and computation through the gateway. |
| Conversation and job state | Persist ownership, permitted history, approvals, job transitions and recovery checkpoints. |
| Durable job execution | Claim jobs, execute work, handle retries and cancellation, and recover interrupted execution. |

These are logical boundaries, not a decision to deploy separate services.
Durability requires both persisted state and execution/recovery logic.

For this branch, tool execution and durable recovery are mocked dependencies.
See [build-order.md](build-order.md) for the implementation sequence.

## Implemented data types

`types/` defines shared JSON values. `runs/` owns trusted run context and lifecycle
snapshots; `conversations/` owns messages and conversation records. `tools/` defines versioned tool
definitions, calls, output policies, visible results, restricted completion
receipts, fixed error codes and pending calls. Artifact
references are separate from the tool-result union. JSON values are copied into
immutable mappings and tuples; adapters must explicitly encode them for transport.

Constructors preserve immutable JSON snapshots and consistent waiting states.
Typed callers supply valid identities, enum members and field types. Future input
adapters and registry/gateway boundaries must validate external data, schemas and
permissions. `runs/state.py` enforces lifecycle transitions when checkpoints are saved.
The `VisibleResult` type must only be populated after output enforcement; its name
does not establish that arbitrary content is safe. `providers/foundry/` translates these
values into SDK-specific messages at the inference boundary.

## Implemented tool registry

`tools/registry.py` provides `ToolRegistry`, which snapshots one selected version
per tool name. `discover()` returns
definitions whose required permissions are all present in the supplied permission
set, sorted by name. `resolve()` requires the exact selected version and checks
the supplied permissions again. Unknown, mismatched and unauthorised lookups
produce the same `ToolUnavailableError`.

Callers obtain current permissions from a trusted provider. Registry results are
internal definitions; the model adapter will select the fields sent to the model.
The gateway delegates resource access and approval decisions to an injected provider
and applies output enforcement after execution.
Registration checks names, versions, duplicate names and the object schema root;
complete JSON Schema validation remains future work. Version updates use a new
registry snapshot with explicit version selection.

## Implemented output enforcement

`policy/output.py` handles completed tool outputs. `enforce_output()` uses the
registered output policy and runtime-supplied call ID. Restricted operations
produce a `RestrictedReceipt`; their payload bypasses the visible filter and is
discarded from the return value. Artifact references use the separate `artifact`
field for authenticated delivery. Only `model_result` enters model context.

Visible operations require a trusted per-operation `visible_filter`. The filter
validates or projects approved fields, and the resulting JSON is snapshotted.
Missing filters, unsupported policies and filter failures produce a fixed
`EXECUTION_FAILED` result. `enforce_failure()` constructs the same outward failure
using the call ID alone; exception details stay with the execution boundary.

Filters are injected dependencies bound through `ToolBinding`; production filters
remain to be built. These functions do not classify sensitive data. The gateway
must bind definitions, filters, call IDs and artifact IDs from trusted context.
Artifact access checks belong to delivery. Pending jobs, approvals and streaming
will be integrated as those execution paths are implemented.

## Implemented tool gateway

`tools/gateway.py` provides synchronous `ToolGateway.dispatch()`. Application
startup supplies a registry, a mapping from `(tool_name, version)` to `ToolBinding`,
and an `AccessPolicy` provider. Tests supply local handlers and a fake provider.
Each binding contains a handler, an argument validator and an optional visible
output filter. This is the execution binding for the selected tool version.

For each call, the gateway:

1. Fetches current permissions and resolves the exact registered version.
2. Finds the binding and checks output-policy configuration before execution.
3. Calls the operation's argument validator; `ValueError` produces a fixed
   `INVALID_ARGUMENTS` result.
4. Checks resource access and consumes approval when the definition requires it.
5. Invokes the handler with trusted run context and immutable arguments.
6. Enforces output policy and returns model output and artifact metadata separately.

The access provider owns entitlement lookup, resource decisions and atomic
approval consumption bound to identity, run, call, version and arguments.
Argument validators supplied by tool developers own schema and domain checks;
a general JSON Schema engine is still pending. Provider and handler exceptions
produce a fixed failure without exposing exception text.

Missing or mismatched approvals return `DENIED`. Approval requests and pending-job
event handling remain future work. Execution is synchronous and has
no retry or duplicate-call recovery; repeated authorised calls can repeat side
effects. Durable execution and replay prevention remain future work.

## Implemented conversation and run storage

`storage/interface.py` defines `StateStore`. Its in-memory implementation stores
owned conversation history and run snapshots. Identifiers are tenant-scoped;
reads and updates check user ownership and the run's full context. Callers supply
identity verified by the application backend.

`create_run()` appends initial input and claims the conversation for one queued
run. `save_checkpoint()` appends permitted history and updates status and pending
calls atomically. Both operations require the expected conversation revision;
checkpoint updates also require the expected run revision. Each write advances
revisions. A stale writer receives `StateConflictError` and must reload before
deciding how to proceed.

For example, a waiting job can record its restricted receipt and resume the run
in one storage operation:

```python
checkpoint = store.load_run(context)
updated = store.save_checkpoint(
    context,
    expected_conversation_revision=checkpoint.conversation.revision,
    expected_run_revision=checkpoint.run.revision,
    status=RunStatus.RUNNING,
    items=(RestrictedReceipt(call_id),),
)
```

The caller must match and claim the outstanding work before making this write.
`ContinuationHandler.resume()` implements matching and duplicate-event rejection.
Pending records store opaque approval or job references; the trusted resolver owns lookup
of their original call/version and current outcome. Approval storage and job
workers remain mocked.

Lifecycle transitions are checked before saving. Waiting runs require unique
pending calls. A continuation temporarily retains them while marked running,
until it saves the resolution. Finished runs release the conversation for a new run. Stored
snapshots are immutable, and a lock prevents partial updates across threads
using the same store instance. Tests exercise competing writers and unchanged
state after rejected updates. `load_run()` returns the requested run together
with the conversation's current history.

History accepts message, call and model-result records. Raw execution results
and artifact delivery references are rejected. Content admission remains the
input/output policy's responsibility. The store has no database connection,
cross-process coordination, crash recovery or durable side-effect guarantees.

## Implemented coordinator loop

`coordinator/runner.py` provides `Coordinator.start()` to create and execute a new
run, and `run()` to execute an existing queued run. `continue_run()` accepts a
claimed checkpoint from the durable layer after pending work has resolved.
Startup injects the model client, gateway, registry, store and a current-permissions
callable. Tests use
scripted model replies and local tool handlers. For example:

```python
result = coordinator.start(
    context,
    Message(MessageRole.USER, "Calculate the total"),
    expected_conversation_revision=conversation.revision,
)
```

Input must already be admitted by the application's input policy. The runner
claims a queued run through a revision-checked transition before invoking the
model. Each model request includes current history and permission-filtered tool
names, descriptions and argument schemas. The runtime retains version, execution
and policy metadata.

The model returns either a final answer or a batch of tool calls. The runner
checks reply shape, unique call IDs and budgets, checkpoints the calls, dispatches
them sequentially through the gateway using the selected versions, then saves
their enforced results. Visible results and restricted receipts enter history;
artifact references remain in `CoordinatorResult.artifacts` for user delivery.
Current permissions are read again on subsequent model steps, and the gateway
independently checks each execution.

The main loop keeps those decisions together. `_request_model()` builds each
request, `reply_validation.py` checks the reply, and `_execute_tools()` checkpoints
one batch and collects its results. The next iteration uses the updated history.

Model-step and tool-call budgets are stored with the run and survive waits and
continuations. Model attempts and tool batches reserve their budget before
execution. An optional cancellation
callable is checked around model calls and between tools. It cannot interrupt a
blocking dependency call. Provider failures produce a failed run with no raw
exception text in history. Storage conflicts propagate to the caller without
automatic retry.

The coordinator accepts `reasoning_effort=ReasoningEffort.HIGH` and forwards it
on every model request, including requests after tool results. The available
settings are `none`, `minimal`, `low`, `medium`, `high`, `xhigh` and `max`;
support depends on the deployed model. The default Python `None` leaves the
provider's default unchanged. `ReasoningEffort.NONE` requests disabled reasoning.
The Foundry adapter checks an explicit allowlist configured for the deployment
and maps the setting to the Chat Completions `reasoning_effort` field.

This controls provider-managed reasoning effort. Chat Completions replays the
permitted transcript on every request. Responses API continuation items and
their persistence remain future work. Tests verify configuration propagation
through the real SDK with a local HTTP transport. See Microsoft's
[reasoning model documentation](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
for model-dependent settings and opaque reasoning state.

A fake dispatcher can return pending approval/job receipts. The runner saves
them, marks the run waiting and returns. The durable continuation handler accepts
their completion notifications. Running, waiting and terminal runs cannot be replayed through
`run()`. Checkpointing does not make side effects durable: a crash during tool
execution still requires reconciliation.

## Implemented Foundry model adapter

`providers/foundry/model.py` implements the model interface using Foundry's v1 Chat
Completions API through the OpenAI SDK. `connection.py` constructs the client
from application-supplied credentials; `messages.py` encodes permitted history
and `replies.py` validates answers and tool requests. See the
[adapter guide](providers/foundry/README.md) for configuration, API mapping and limits.

Tests exercise the SDK through a local HTTP transport, including the complete
calculation, approval, report and continuation workflow. Live deployment
compatibility and Azure inference remain untested.

## Durable execution scaffolding

`durable/continuation.py` owns event definitions, matching and the resolver
interface. `durable/handler.py` owns `ContinuationHandler`, which claims waiting
runs, resolves events and invokes the coordinator when their results are ready.
The coordinator has no dependency on `durable/`.

Startup gives the handler the shared store, coordinator and a trusted
`ContinuationResolver`; tests supply local job and approval fakes. A notification
contains the call ID, wait reason and opaque provider reference:

```python
from agent_runtime.durable.continuation import ContinuationEvent
from agent_runtime.durable.handler import ContinuationHandler

continuations = ContinuationHandler(store, coordinator, resolver)
result = continuations.resume(
    context,
    ContinuationEvent("call-2", WaitReason.JOB, "job-17"),
)
```

The transport supplies verified `context`. The continuation handler checks ownership,
matches the notification to the run's pending calls, then claims the waiting run
using its stored revisions before invoking the resolver. A second consumer cannot
claim the same snapshot. After resolution, that pending reference is removed;
duplicate, mismatched and late notifications are rejected with `StateConflictError`.
Concurrent notifications for different jobs may conflict too; the caller can
retry the unresolved event after the current consumer releases the run.

The event carries no approval decision or result payload. For an approval, the
resolver loads the original call and pinned version and uses the gateway, which
checks current permissions and consumes the bound approval. For a completed job,
the resolver rechecks current access and applies output policy before returning
`EnforcedOutput`. These provider responsibilities remain mocked; production
authentication, approval storage, job lookup and policy wiring are still required.

An approval may return a new job receipt, keeping the run waiting. With several
pending jobs, each completion saves its result and the last completion resumes
the model loop. The next request includes all saved permitted results and current
tool discovery. Adapter exceptions or mismatched output IDs produce a fixed tool
failure. Cancellation is checked before and after resolution.

Each invocation returns its newly produced artifact references separately from
model history. The application must retain them for authenticated delivery;
persisted delivery metadata remains future work. Tests exercise out-of-order
completion, duplicate consumers, approval denial, permission revocation, output
separation and budget limits across resumed runs.

Execution remains synchronous. Async dependencies, concurrent tool dispatch,
Kafka transport and durable recovery remain unimplemented. A crash after claiming
a wait can leave the run running, and a crash after execution still needs
side-effect reconciliation. No automatic recovery or replay of such runs is provided.

## End-to-end integration harness

`tests/integration/` connects the real registry, gateway, output enforcement,
in-memory store, coordinator and durable continuation handler. Synthetic providers
supply model replies, permissions, approval decisions, delayed job results and
report delivery. See the [harness guide](../../tests/integration/README.md) for its
scope and commands.

The main workflow uses a visible calculation result to request a restricted
report, waits for approval, resolves a job notification and produces the final
reply. Additional scenarios exercise denied calls, permission/resource changes,
failure sanitisation, cancellation, mixed pending calls and interleaved sessions.
Every scenario inspects actual model-request fields and stored history, including
fields hidden by `repr=False`, for the synthetic restricted-payload marker and
artifact identifiers. Report content is read separately through a fake delivery
boundary that checks ownership and current access.

These are local tests with scripted inference and immediate execution whose
result delivery can be delayed. Live Foundry inference, async dispatch, Kafka,
persistent storage and production authentication remain untested.

## Workshop questions

For each component, define its inputs and outputs, ownership of state, calls to
other components, failure behaviour and permission checks. Then map those needs
to service choices and concrete API calls, distinguishing proposals from verified
SDK behaviour and executed evidence.

Start with one synthetic request through discovery, authorised tool execution and
a visible result. Extend that flow to a restricted result and protected artifact,
then asynchronous execution and recovery. Do not expose restricted data through
model context, errors or job status.

Browser UI, enterprise identity integration and report delivery are surrounding
dependencies; their runtime-facing boundaries belong in this workshop. Raw chat
input policy remains unresolved.

See [components.md](../../components.md) for the full component catalogue and
[ARCHITECTURE.md](../../ARCHITECTURE.md) for the proposed architecture.

## Code layout

```text
agent_runtime/
  coordinator/
    model.py          # Model request/reply values and client interface
    reply_validation.py # Reply shape and tool-call ID checks
    control.py        # Execution budgets and application-facing outcome
    runner.py         # Run entry point and bounded model/tool loop
  providers/
    foundry/
      connection.py   # SDK connection and supplied credentials
      model.py        # One Foundry inference call per coordinator step
      messages.py     # Permitted history and tool results to API messages
      replies.py      # Provider completion and tool-argument validation
  durable/
    continuation.py   # Completion events, matching and trusted resolver interface
    handler.py        # Claim waiting runs, resolve events and resume the coordinator
  conversations/
    conversation.py   # Owned conversation record and history item union
    messages.py       # Conversation text and message roles
    history.py        # History admission checks
  runs/
    context.py        # Trusted tenant, user, conversation and run identity
    state.py          # Run statuses, snapshots and transition rules
  storage/
    interface.py      # Atomic storage operations and checkpoint snapshots
    memory.py         # In-memory store with revision checks and a lock
  helpers/
    json_snapshot.py  # Immutable JSON conversion
  policy/
    access.py         # Permission, resource-access and approval provider interface
    output.py         # Completed-output enforcement and separate delivery metadata
  tools/
    definitions.py    # ToolDefinition, ToolCall and output policy
    bindings.py       # Per-version handlers, argument validators and output filters
    results.py        # Tool outcomes, status codes and artifact references
    registry.py       # Permission-filtered discovery and version lookup
    gateway.py        # Authorisation, synchronous dispatch and output enforcement
    _registration.py  # Registration validation helper
  types/
    json_value.py     # Recursive JSON value alias
```

Import values from their defining modules, for example
`from agent_runtime.tools.definitions import ToolDefinition` and
`from agent_runtime.tools.registry import ToolRegistry`.
