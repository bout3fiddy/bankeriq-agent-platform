# Build order

Build from core runtime logic towards external integrations. These steps are
planned except where marked implemented. Keep external I/O mocked until the adapter steps.

The initial runtime scope (steps 1–13) is complete as of 2 November 2026,
including live-model validation with synthetic integrations. Containerisation,
durability, storage, message passing and hosted execution form the next phase on
`codex/runtime-infrastructure`.

1. **Core data types (implemented)** — Run context, messages, tool definitions, calls, results,
   output policies and run statuses.
2. **Tool registry (implemented)** — Register versioned definitions and schemas; discover tools
   filtered by permissions.
3. **Output enforcement (completed outputs implemented)** — Produce model-visible results or restricted receipts;
   sanitise failures and route artifact references separately.
4. **Tool gateway (synchronous dispatch implemented)** — Validate calls, check permissions and approvals, dispatch
   through a mock executor, and enforce output policy.
5. **Conversation and run state (implemented)** — Define storage interfaces and an in-memory
   implementation for history, pending calls, checkpoints and waiting states.
6. **Coordinator loop (implemented)** — Assemble context, call the selected model, handle tool
   requests and finish or suspend within execution limits.
7. **Durable continuation handling (implemented with mocked providers)** — Under
   `durable/`, match approval/job events to pending work, claim resolution,
   reject duplicates and resume after
   all outstanding calls resolve. Preserve budgets across waits. Durable recovery
   remains future work.
8. **End-to-end test harness (implemented with synthetic I/O)** — `tests/integration/`
   connects the runtime components through visible and restricted outputs, denied
   calls, failures, cancellation, waiting and continuation. Checks actual model
   request fields and stored history for synthetic restricted payloads and artifact IDs.
9. **Foundry model adapter (implemented; local and live tests)** — `providers/foundry/` maps
   permitted history and discovered tools to v1 Chat Completions through the
   OpenAI SDK, then validates answers and function calls. Live Azure inference
   passed with regional `gpt-4o` version `2024-11-20` in Sweden Central.
10. **Hosted-agent adapter (implemented; local ASGI tests)** — `hosting/` handles
    validated application commands; `providers/foundry/hosting.py` exposes them through
    Microsoft's Invocations SDK. Authentication and input admission are required
    injected dependencies, supplied by local fakes in tests. Azure identity
    integration, deployment and lifecycle validation remain pending.
11. **Provider selection (implemented; local tests)** — Keep SDK integrations
    under `providers/`; select model and hosting factories independently at
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
    [Azure resource inventory](../../infra/azure/README.md).

Add meaningful component tests alongside each step; step 8 brings those components
together for end-to-end validation.

## Next phase: runtime infrastructure

- **Containerisation (planned)** — Package the runnable application with explicit
  configuration, dependency versions, readiness and graceful shutdown.
- **Persistent state and durability (planned)** — Add persistent conversation,
  run and job storage; implement worker ownership, recovery, bounded retries and
  idempotency around execution.
- **Message passing (planned)** — Connect runtime requests and worker completion
  events through a transport, preserving authorisation and output enforcement.
- **Blob storage and delivery (planned)** — Store report artifacts privately,
  record ownership and require authenticated access for downloads.
- **Hosted runtime (planned)** — Publish the image and provision hosting,
  identities and supporting resources through Terraform. Validate authentication,
  input admission, readiness, concurrent sessions, shutdown and recovery in Azure.

## Follow-up runtime work

- **Context management (planned)** — Add input-context budgeting and compaction
  while preserving instructions, tool-call/result relationships and output policy.
- **Streaming (planned)** — Carry incremental model responses and runtime events
  through provider adapters and hosting, with output enforcement and cancellation.
- **Parallel tool execution (planned)** — Add async dispatch for independent calls,
  bounded concurrency and result collection before the next model step. Preserve
  per-call authorisation, output enforcement and consistent state updates.

## Scope boundary

Real tool applications, durable workers, persistent storage, enterprise identity
integration and the browser app remain outside the completed initial core scope.
Permission checks and output enforcement are real runtime logic; their external
providers and execution dependencies are mocked. The self-hosted specialist is
deferred.
