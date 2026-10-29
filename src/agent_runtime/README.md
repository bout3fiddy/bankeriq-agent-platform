# Agent runtime workshop

Workshop and implementation workspace for the core agent runtime. Core data types
are implemented; the coordinator, gateway and external APIs are not yet implemented.
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

`types/` defines trusted run identity, text messages, versioned tool definitions,
untrusted tool calls, output policies, visible results, restricted completion
receipts, fixed error codes, pending calls and lifecycle snapshots. Artifact
references are separate from the tool-result union. JSON values are copied into
immutable mappings and tuples; adapters must explicitly encode them for transport.

Constructors preserve immutable JSON snapshots and consistent waiting states.
Typed callers supply valid identities, enum members and field types. Future input
adapters and registry/gateway boundaries must validate external data, schemas and
permissions. Lifecycle transition enforcement remains separate work.
The `VisibleResult` type must only be populated after output enforcement; its name
does not establish that arbitrary content is safe. No SDK-specific messages or
serialization format are fixed yet.

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

The type package groups definitions in `enums.py`, `json_value.py`, `messages.py`,
`tools.py`, `results.py` and `runs.py`. JSON snapshot helpers live in `_json.py`. Public imports remain
available through `agent_runtime.types`.
