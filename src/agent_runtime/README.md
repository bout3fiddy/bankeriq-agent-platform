# Agent runtime workshop

Workshop and implementation workspace for the core agent runtime. Core data types,
tool registry, completed-output enforcement, synchronous tool gateway and in-memory state storage
are implemented; the coordinator and external APIs are not yet implemented.
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
does not establish that arbitrary content is safe. No SDK-specific messages or
serialization format are fixed yet.

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

The caller must match the completion event to outstanding work before making
this write. Event matching, duplicate-event handling and approval consumption
are separate integration steps. Pending records currently store opaque approval
or job references; no approval database or job worker is implemented.

Lifecycle transitions are checked before saving. Waiting runs require unique
pending calls; finished runs release the conversation for a new run. Stored
snapshots are immutable, and a lock prevents partial updates across threads
using the same store instance. Tests exercise competing writers and unchanged
state after rejected updates. `load_run()` returns the requested run together
with the conversation's current history.

History accepts message, call and model-result records. Raw execution results
and artifact delivery references are rejected. Content admission remains the
input/output policy's responsibility. The store has no database connection,
cross-process coordination, crash recovery or durable side-effect guarantees.

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
