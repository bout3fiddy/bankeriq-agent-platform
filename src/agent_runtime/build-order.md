# Build order

Build from core runtime logic towards external integrations. These steps are
planned except where marked implemented. Keep external I/O mocked until the adapter steps.

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
6. **Coordinator loop (implemented)** — Assemble context, call a scripted mock model, handle tool
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
9. **Foundry model adapter** — Replace the mock model with real inference calls
   behind the same interface.
10. **Hosted-agent adapter** — Expose the coordinator through Foundry's
    request/response protocol and map trusted request context into runtime inputs.

Add meaningful component tests alongside each step; step 8 brings those components
together for end-to-end validation.

## Scope boundary

Real tool applications, durable workers, persistent storage, enterprise identity
integration and the browser app remain outside this branch's initial scope.
Permission checks and output enforcement are real runtime logic; their external
providers and execution dependencies are mocked. The self-hosted specialist is
deferred.
