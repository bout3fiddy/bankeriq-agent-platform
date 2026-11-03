# Agent harness

Agent behaviour and policy independent of hosting, database/cache clients and provider SDKs.
The harness owns the model/tool loop, current permission checks, output enforcement, context
selection, prompt compilation and the interfaces that execution adapters implement.

The [runtime](../agent_runtime/README.md) implements persistence, provider/hosting transport,
durable workers, sessions and recovery. The [platform](../agent_platform/README.md) supplies
application configuration, identity, prompts and tool bindings. Dependencies point from the
platform to runtime/harness and from runtime to harness. This package imports neither layer.

## Components

- `coordinator/`: `Coordinator` prepares model requests, validates replies and enforces run
  limits. The synchronous loop remains available; the queued runtime reuses its model-step
  preparation and gateway while persisting separate jobs for waits and tool children.
- `tools/`: versioned definitions, permission-filtered discovery, validated arguments and
  gateway execution through injected handlers. Model suggestions cannot grant authority.
- `policy/`: current access interfaces and completed-output enforcement. Restricted results
  become fixed receipts; authenticated delivery references stay separate from model history.
- `conversations/`: immutable messages/history and bounded complete-exchange context helpers.
- [Memory](memory/README.md): token budgets, source-linked facts, compaction selection,
  retirement validation, visibility projection and bounded history-tool definitions.
- [Prompts](prompts/README.md): immutable manifests, composition, strict authoring schema and
  pure catalog compilation. Application startup selects files; runtime adapters load them.
- `runs/`: trusted tenant/user/run context, lifecycle state and pending-call invariants.
- `continuations/`: match a completion to the exact pending call and resume a checked
  checkpoint. The runtime owns distributed leases, scheduling and transactional joins.
- `storage/`: `StateStore` protocol, checkpoint/history types and the bounded journal codec.
  Runtime supplies [PostgreSQL](../agent_runtime/storage/README.md) and in-memory adapters.
- `types/` and `helpers/`: shared JSON values and immutable snapshot helpers.

## Execution and output boundaries

Application startup injects the model interface, store, registry, dispatcher and current
permission reader. The coordinator prepares instructions/history/tools, reserves its step
budget and accepts a final answer or validated tool calls. Each tool is checked again by
the gateway. Visible results enter model history after projection; raw restricted payloads
and report bytes remain inside the producing tool's boundary.

A synchronous dispatcher can return a pending approval/job receipt. `ContinuationHandler`
matches its call ID, reason and reference before resolving a permitted outcome. Matching a
notification provides no distributed ownership by itself. The
[queued profile](../agent_runtime/workers/asynchronous.md) adds atomic child admission,
leases, cancellation, recovery and one durable join continuation.

For state transitions, the caller supplies expected conversation/run revisions. A stale
revision fails explicitly. The `StateStore` implementation atomically saves permitted history,
state, budgets and separate delivery metadata. The local in-memory adapter covers one
process lifetime; PostgreSQL workers provide the documented durable recovery guarantees.

Memory policy receives bounded source windows and current access decisions. It preserves
source attribution and tool protocol, selects complete exchanges and validates generated
selections. The runtime reserves summary work, invokes the provider and checks ownership/
policy/generation before publication. User claims cannot become approvals or executed actions.

## Validation and extension

Unit tests under `tests/unit/` exercise decisions and invariants using synthetic inputs.
[Integration tests](../../tests/integration/README.md) cover real local PostgreSQL/Redis,
independent workers, provider HTTP fakes, restricted-output isolation and recovery.
Run `uv run python scripts/verify.py` from the repository root with local services configured.
Installed-wheel/container validation additionally checks packaged assets and startup wiring.

Add business operations through reviewed tool bindings and schemas. Put provider SDKs and
credentials in runtime adapters, application identity/configuration in the platform, and
browser code in `frontend/`. Preserve existing interfaces and explain any new abstraction.
