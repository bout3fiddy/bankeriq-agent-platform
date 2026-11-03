# Asynchronous coordinator and tools

`compose_queued_runtime()` is the explicit PostgreSQL profile. Application startup
owns the async model client's lifetime, current permission source, registry, gateway
and trusted child binder. `open_runtime()` retains the synchronous profile. Configure
`model_prepare`, `model_infer` and each tool queue before starting `AsyncWorker`.

A preparation job reserves one model step and saves its prompt manifest. Inference
loads that exact prepared step. Provider retries consume the original job's bounded
attempt allowance and run-wide worker budget. A newly prepared step resolves the
current prompt binding. `AsyncFoundryModelClient` uses the same wire validation as
the synchronous adapter, disables SDK retries and enables parallel function calls.
The existing `runtime.tool-protocol@1.0.0` prompt already instructs independent batching,
distinct IDs and waiting for dependent inputs; its immutable asset remains unchanged.

`QueuedCoordinator.bind_tool` pins each offered version, call ID and permitted
arguments. Trusted applications may replace that binder to select queue/replay policy,
a per-call attempt limit and a resource key. Resource keys are tenant scoped and
serialize conflicting effects across conversations; a shared tool name imposes no
ordering. Unknown effects retain the resource until resolved. Queue and tenant limits
bound parallelism across independent worker processes.

The binder must keep the parent's run budget. It must never derive replay permission,
failure policy or resource-lock authority directly from untrusted model fields. An
application can derive a resource key from a validated account identifier using its
trusted conflict policy. The gateway repeats schema, resource permission and approval
checks immediately before execution. Its durable path propagates uncertain exceptions
to worker recovery without recording raw text. `ToolBinding.durable_handler` receives
the original call and stable operation ID for provider idempotency. Pure calculations
can use the ordinary blocking handler; effectful replay needs a compatible adapter.

Migration 011 adds group ownership, ordered child positions, resource keys and bounded
child outcomes to the existing jobs. Model intent, waits, child enrollment and coordinator
settlement share one transaction. Child result/settlement commits independently, leaving
the parent's revision stable. The last child uses `ContinuationHandler.child_outcome`
validation and appends ordered results, delivery metadata and one continuation in the
same transaction. Earlier child outcomes are durable in jobs and visible through scoped
inspection; the SSE refreshed view includes their safe status before journal joining.
A duplicate result cannot create another continuation. A conflicting duplicate fails.

`wait_all` is the default sibling policy. A trusted `fail_fast` selection fences unfinished
siblings and finishes the parent with failure. Cancellation serializes against enrollment,
claims and settlement. Dispatched siblings remain `cancelled_unknown`; cancellation cannot
undo their effects. Neither late results nor operator commands reopen a terminal parent.
Known child failures join as fixed-code outcomes. Unknown failures stay stopped/reconciling
and keep the group waiting. Existing authorised `RecoveryControls` retain budgets,
operation IDs, crash accounting and audit decisions; inspection adds parent/group/call IDs.

Approval-required calls pause before dispatch, releasing their worker slot. Trusted approval
must bind the saved recovery envelope through `queue.approve()` and satisfy the gateway's
current exact-operation approval policy. Waiting expires after 300 seconds in this initial
composition; queue primitives support other trusted deadlines. Custom remote-job tools must
register a continuation/lookup adapter before use. The stock tool adapter expects a terminal
gateway result; returning an unsupported pending result preserves an unknown effect for
explicit recovery. The synchronous continuation profile remains available unchanged.

`AsyncWorker.run_async()` bounds active tasks. It awaits async providers and isolates
blocking tools/database calls in executor work. A waiting conversation owns durable rows
without an active task or thread. CPU-heavy integrations supply an application-owned process
adapter through `Handler.async_execute`, or use independent dedicated worker processes.
Finite provider/handler timeouts and deployment process limits remain application settings.
The worker connection-budget guard also applies to this profile.

`create_async_app()` provides authenticated `POST /runs` and `GET /events` routes. The
injected authenticator supplies the scoped context; the input policy runs before admission.
The accepted run ID is returned only after the PostgreSQL commit. SSE has a bounded
connection-local byte/event queue. Overflow discards queued data, sends `reconnect` and
closes; the client reconnects with its last delivered durable `Last-Event-ID` cursor.
Cursors include recovery/deletion generations. One bounded journal event is fetched per
poll; client sockets never hold database transactions or worker slots.

Redis stores at most 32 disposable progress entries, with model token fragments bounded
to 1 KiB. Run admission generations and attempts fence older producers. Tokens and progress
never drive execution or final acknowledgement. Redis loss rebuilds from PostgreSQL; final
answers are delivered as saved journal events. Opaque tool-owned report links travel in
separate delivery metadata and never enter model receipts. The refreshed view includes
safe child status and unknown-effect flags, allowing recovery even before a group joins.

## Validation

See [group 07](../../../build-order/07-completed-asynchronous-coordinator-and-tools.md)
for executed commands, versions and acceptance evidence. Tests use synthetic tools and
local SDK transports, real PostgreSQL/Redis and independent worker processes. No cloud
resources, live provider calls, browser application or production identity are required.
Hosted scaling, HA/PITR, deployment, production timeouts and throughput targets remain
subsequent work. External effects require provider idempotency or explicit reconciliation;
there is no exactly-once external-effect claim.
