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
5. **Conversation and run state** — Define storage interfaces and an in-memory
   implementation for history, pending calls, checkpoints and waiting states.
6. **Coordinator loop** — Assemble context, call a scripted mock model, handle tool
   requests and finish or suspend within execution limits.
7. **Continuation handling** — Accept mock approval/job events, match them to
   pending work, reject duplicates and resume the coordinator. This does not
   provide durable recovery.
8. **End-to-end test harness** — Exercise visible and restricted outputs, denied
   calls, failures, cancellation, waiting and continuation; verify restricted data
   never reaches model context.
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
