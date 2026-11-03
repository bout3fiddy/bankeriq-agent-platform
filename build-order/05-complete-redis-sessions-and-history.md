# Redis sessions and asynchronous history

Status: complete and locally validated. Depends on PostgreSQL records and shared system prompts.

1. **Serve active views** — Hydrate recent context from a PostgreSQL snapshot plus
   journal tail, then serve API reads from tenant/conversation Redis keys. Page old
   history. Track schema, generation and durable cursor; reject stale refills and
   delayed updates. Test reads that require the caller's last accepted cursor.
2. **Separate pending and accepted state** — Allow immediate provisional UI state
   in Redis. Return durable acceptance only after the PostgreSQL admission commit;
   model/tool execution cannot start from Redis-only input. Reconcile pending
   display state by request ID after retries and reconnects. Record projection work
   with each durable transition; a failed cache refresh cannot undo acceptance.
3. **Project history asynchronously** — Record dirty cursors in PostgreSQL and
   materialise bounded history pages/snapshots in batches. Starting triggers are
   60 seconds, 100 events or 1 MiB; these require measurement. Persist cursor advance
   atomically and preserve events that arrive during projection. Version the JSON
   snapshot shape and rebuild from its committed cursor plus subsequent deltas;
   verify this produces the same permitted view as replaying the retained journal.
4. **Handle idle cleanup** — Propose a 15-minute idle snapshot and 30-minute sliding
   Redis TTL. Discover dirty/idle work from PostgreSQL even when Redis is gone.
   Explicit cleanup compares the observed generation/cursor before deleting keys.
   Retain journal records until durable materialisation and all recovery/retention
   references permit deletion.
5. **Bound context and memory** — Keep only recent context and bounded progress
   streams in Redis. Preserve tool-call/result pairs and fail explicitly when the
   model context budget cannot be met. Semantic compaction remains follow-up work.
6. **Validate loss and races** — Delete Redis before idle, during a tool wait and
   after admission but before refresh. Recover accepted history; test concurrent
   hydration, stale cache writes, busy conversations that never idle, deletion
   tombstones and cleanup racing with a new turn. Crash after cache update before
   projection acknowledgement; retries must preserve newer cursors. Reject cache
   records from an earlier database recovery generation.

Completion requires Redis to be disposable and large conversations to avoid
synchronous full-history writes. Persisting small durable deltas remains required.
The projection loop runs explicitly until durable workers exist. The
[session guide](../src/bankeriq_agent_platform/sessions/README.md) documents API wiring,
limits, projection/cleanup loops, retention and reproducible container commands.
Immutable prompt bodies also have an optional Redis cache; primary checks remain required.

Implemented: bounded snapshots/pages, durable dirty discovery, cursor/generation fences,
provisional reconciliation, recent complete-turn context and explicit context overflow.
Local synthetic tests cover Redis loss, restart, concurrent hydration, stale updates,
projection rollback/unknown commits, tenant/output isolation, busy conversations,
idle races, fair projection scans and immutable prompt cache corruption/withdrawal.
No automatic scheduler, worker execution ownership or journal pruning is added.

Executed on 3 November 2026: the full verification gate passed 324 tests and static
checks with PostgreSQL 17.11 and Redis 7.4.12 enabled, including 21 session scenarios.
The rebuilt synthetic container passed its offline/HTTP/shutdown checks; wheel
inspection confirmed packaged Lua and migrations. The session guide records local
read/projection samples and limits. No cloud or live-model calls were made.
