# PostgreSQL journal and job records

Status: completed and validated locally on PostgreSQL 17.11. Depends on containerisation.

1. **Map existing storage operations** — Review `StateStore`, run transitions and
   `open_runtime()` before adding focused transactional queue methods. Define
   versioned conversation/event, checkpoint, job, result and approval records;
   map each transition to the architecture's atomic-write table. Keep clients,
   credentials and wire parsing outside core logic. Include tenant/conversation
   scope in related records; the initial adapter routes to one writable primary.
2. **Admit durable work atomically** — Append the actual permitted message delta,
   assign its conversation sequence, bind a stable tenant/request ID to its input
   fingerprint and create work in one transaction. Resolve unknown commits by ID.
   Keep one active logical run; later accepted turns queue in order and stay outside
   the current run's input context. Separate admission sequence from execution
   revision so accepting another message does not invalidate an active model call.
3. **Checkpoint bounded deltas** — Persist model/tool budgets, committed outcomes,
   delivery metadata and next work together. Keep confidential content out of
   shared records. Store large history in bounded pages; avoid rewriting the entire
   conversation on each message.
4. **Add schemas and indexes** — Version migrations and checkpoint formats. Use
   unique operation/event IDs, tenant-scoped lookups and partial indexes for due
   jobs/expired leases. Start with `READ COMMITTED`, explicit aggregate locks,
   consistent lock order and conditional revision checks. Define retention,
   deletion and database recovery generations before cleanup.
5. **Validate persistence** — Use a real local PostgreSQL container for rollback,
   restart, duplicate admission, stale revision, queued-turn ordering and independent
   process tests. Inject failure between writes in each implemented transaction
   and after commit before acknowledgement. Resolve retries by ID on the primary;
   check cross-tenant denial and output isolation.

Completed: accepted input and queued work survive a process restart together.
Job execution remains synthetic until the durable worker group. The
[storage guide](../src/bankeriq_agent_platform/storage/README.md) maps implemented
transactions, bounds, retention prerequisites, generation fences and limitations.
Versioned migrations and the adapter live under `src/bankeriq_agent_platform/storage/`;
`open_runtime()` accepts the adapter through its existing store dependency.
The default executable remains in-memory.

Evidence: all 257 Python tests and static checks passed with PostgreSQL enabled,
including 13 PostgreSQL integration tests covering write-boundary rollback,
independent process admission/recovery, lost commit acknowledgements, duplicate
and conflicting IDs, stale revisions, ordered context, approvals, tombstones,
recovery generations and restricted-output isolation. Run the reproducible Docker
commands in the storage guide. Automatic recovery of running execution, worker
ownership and production HA remain later groups. Projection was added in the Redis group.
