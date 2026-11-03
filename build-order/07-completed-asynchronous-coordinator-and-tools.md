# Asynchronous coordinator and tool queues

Status: completed. Depends on durable workers. This is part of the infrastructure
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
   recovery owner. Persist each child's parent run/group and its failure policy.
   Default to waiting for every child; a trusted fail-fast policy may cancel
   unfinished siblings. Successful parent completion requires the required child
   outcomes. Fence pending children and reject new child admission when their
   parent is cancelled or terminal. Retain dispatched unknown effects for
   reconciliation; late results cannot reopen the parent or create a continuation.
4. **Integrate worker recovery controls** — Use the failure reasons, crash limits
   and authorised recovery controls from the
   [worker group](06-completed-durable-postgresql-workers.md#worker-recovery-extensions)
   for model-step and tool jobs. Carry their safe status into run inspection and
   continuation decisions; preserve child ownership, committed outcomes and the
   worker's operation identity across an authorised recovery.
5. **Keep API and workers responsive** — Return an accepted run ID; release the
   coordinator slot during tool/approval waits. Use bounded async I/O, isolate
   blocking handlers and CPU-heavy tools, and preserve model/tool budgets across
   resumption. No thread remains allocated to an idle chat.
6. **Deliver progress and final results** — Add bounded SSE/progress events through
   Redis with run/attempt generations. Reconnect uses a durable cursor and rebuilds
   after stream loss. Persist final answers before saved acknowledgement. Forward
   synthetic tool-owned report links separately from model receipts. Label partial
   tokens and tool progress as disposable; they cannot drive durable decisions.
   Admission, intent, checkpoints and final outcomes always require a PostgreSQL
   commit. Test that losing displayed progress preserves all accepted work. Bound
   each connection's queued bytes/events and coalesce disposable progress while a
   client is slow. On overflow, replace queued progress with a refreshed view or
   require reconnect from a durable cursor. Recover committed outcomes from that
   cursor; a full client buffer must never hold a worker or database transaction.
7. **Validate async behaviour** — A slow tool must not block another conversation.
   Test simultaneous tool completion, partial failure, cancellation, revoked access,
   client disconnect, lost token stream and interrupted model calls. Check that
   confidential payload markers never enter model context, journal or traces.
   Use barriers to prove overlap for multiple calls to the same tool. Include
   distinct IDs with identical arguments, duplicate delivery of one ID, resource
   serialization, per-call budgets and out-of-order results with one continuation.
   Cancel while children are admitted, claimed and settling: every child retains
   an owner, no later child dispatch or continuation is admitted, and unknown
   effects remain inspectable. Test both sibling-failure policies. Verify worker
   failure reasons and authorised recovery propagate through model/tool groups
   without leaking raw errors or scheduling duplicate continuations. Stall an SSE
   consumer beyond its buffer limit and prove bounded memory, independent execution
   and recovery of the final outcome.

Completion requires a real local PostgreSQL/Redis conversation to fan out synthetic
jobs, release its coordinator, resume once and survive a worker restart. Streaming
transport tests do not require implementing the deferred browser application.
The acceptance gate also includes child cancellation races, integration with worker
recovery controls and bounded slow-client delivery.
These gates passed locally; executed evidence follows.

## Implementation and acceptance evidence

Implemented on `codex/runtime-infrastructure` from baseline `1216460` on 3 November 2026.
Migration 011 extends the existing jobs with child/group ownership and trusted resource keys.
Migrations 001–010 remain unchanged. See the
[asynchronous profile](../src/bankeriq_agent_platform/workers/asynchronous.md) for composition,
transaction boundaries, approval handling, recovery and SSE transport details.

The acceptance suite is `tests/integration/test_async_groups.py`; its independent subprocess
entry point is `tests/integration/async_process.py`. Executed scenarios cover:

- Three same-tool calls with identical arguments and distinct IDs overlap behind a barrier.
  Reversed completion order and duplicate settlement produce ordered history and one join.
- Two independent worker processes claim tools concurrently. Killing one after dispatch,
  restarting another and completing the survivors produces one durable continuation.
- Model preparation releases its job before inference. Interrupted local SDK transport retries
  retain the original prompt manifest and model-step counter; SDK request batching is checked.
- Slow async model/tool work permits another conversation to complete. Approval waits release
  capacity and preserve the exact call identity. Model and tool budgets remain bounded.
- Cancellation races atomic enrollment and final settlement, and covers admitted, claimed
  and dispatched children. Unknown effects remain inspectable; no later continuation runs.
- Both sibling policies, revoked permissions, per-child crash limits, resource serialization,
  scoped inspection and authorised audited child recovery retain parent and operation IDs.
- Failed enrollment rolls back intent and all children. A retry can commit the same batch.
- Restricted payload markers stay out of model requests, journal, results and job records.
  Synthetic tool-owned report links remain separate from model receipts.
- Client disconnect and Redis/token-stream loss preserve accepted work. A genuinely stalled
  async SSE reader overflows its bounded queue while tools and final completion proceed.
  Reconnect from durable cursors recovers the committed final answer.
- HTTP admission validates input, returns 202 only after commit and deduplicates retries.
  Redis progress rejects old attempt generations and labels token fragments disposable.

Local services: PostgreSQL 17.11, Redis 7.4.12, Python 3.14.8. Reproduce with disposable
containers and isolated test schemas (the Redis test database is disposable):

```sh
docker run -d --name bankeriq-group07-postgres \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55437:5432 postgres:17
docker run -d --name bankeriq-group07-redis \
  -p 127.0.0.1:56387:6379 redis:7
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55437/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56387/0'
uv run pytest -q tests/integration/test_async_groups.py
uv run python scripts/verify.py
git diff --check
```

Final verification: `uv run python scripts/verify.py` passed all static gates and
428 tests in 46.47 seconds, with PostgreSQL/Redis enabled and no skips. The group adds
27 orchestration acceptance cases. `git diff --check` passed. No cloud resources were
provisioned or deployed. Real external tool implementations, enterprise authentication,
HA/PITR, hosted scaling and throughput targets remain outside this group. Tool-owned
remote-job integrations require compatible registered continuation/lookup adapters;
unknown external effects remain explicit until provider evidence resolves them.
