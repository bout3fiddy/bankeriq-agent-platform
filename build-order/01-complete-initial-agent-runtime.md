# Initial agent runtime

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
    [Azure resource inventory](../infra/azure/README.md).

Add meaningful component tests alongside each step; step 8 brings those components
together for end-to-end validation.
