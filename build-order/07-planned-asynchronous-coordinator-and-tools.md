# Asynchronous coordinator and tool queues

Status: planned. Depends on durable workers. This is part of the infrastructure
milestone; parallel tool execution has moved out of follow-up scope.

1. **Extract resumable model steps** — Adapt the existing coordinator loop to commit
   one model-step outcome and its next work. Retain the in-memory synchronous
   profile as a reference. Add an async provider dependency at the model boundary;
   keep SDKs, pools and worker scheduling outside core runtime logic. Resolve and
   pin the prompt manifest when preparing each new step; retries reuse it. Use the
   worker group's versioned phase records to reconstruct each model/tool boundary;
   recovery invokes the saved phase and may repeat its unfinished external call.
2. **Queue tool calls** — Commit validated model output, pinned tool intent and
   child jobs together before dispatch. Parallel execution is the bounded default
   for every call in the reply, including repeated calls to the same tool. Explicit
   trusted resource-conflict rules can require ordering; a shared tool name alone
   cannot. Document batching in model instructions and validate provider support.
   Dependent calls belong in later model steps after their inputs arrive. Preserve
   distinct call IDs even for identical arguments; retries reuse their original
   identity. Tool workers recheck access and approvals for each operation.
3. **Join results once** — Record each outcome and job settlement atomically with
   its pending group. Serialize the group update and uniquely key the resume job.
   Preserve tool-result order in model context while allowing out-of-order UI
   progress. Adapt the existing continuation handler rather than creating a second
   recovery owner.
4. **Keep API and workers responsive** — Return an accepted run ID; release the
   coordinator slot during tool/approval waits. Use bounded async I/O, isolate
   blocking handlers and CPU-heavy tools, and preserve model/tool budgets across
   resumption. No thread remains allocated to an idle chat.
5. **Deliver progress and final results** — Add bounded SSE/progress events through
   Redis with run/attempt generations. Reconnect uses a durable cursor and rebuilds
   after stream loss. Persist final answers before saved acknowledgement. Forward
   synthetic tool-owned report links separately from model receipts. Label partial
   tokens and tool progress as disposable; they cannot drive durable decisions.
   Admission, intent, checkpoints and final outcomes always require a PostgreSQL
   commit. Test that losing displayed progress preserves all accepted work.
6. **Validate async behaviour** — A slow tool must not block another conversation.
   Test simultaneous tool completion, partial failure, cancellation, revoked access,
   client disconnect, lost token stream and interrupted model calls. Check that
   confidential payload markers never enter model context, journal or traces.
   Use barriers to prove overlap for multiple calls to the same tool. Include
   distinct IDs with identical arguments, duplicate delivery of one ID, resource
   serialization, per-call budgets and out-of-order results with one continuation.

Completion requires a real local PostgreSQL/Redis conversation to fan out synthetic
jobs, release its coordinator, resume once and survive a worker restart. Streaming
transport tests do not require implementing the deferred browser application.
