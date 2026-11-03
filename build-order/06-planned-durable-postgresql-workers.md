# Durable PostgreSQL workers

Status: planned. Depends on PostgreSQL records and Redis recovery semantics.

1. **Define serializable recovery state** — Extend the existing storage codec and
   versioned checkpoint records with explicit task kind/version, phase, bounded
   inputs or references, operation identity, waits and retry state. Encode strict
   JSON at the adapter boundary; reject unsupported values and non-finite numbers
   before writes. Persist permitted data only; keep callables, credentials, clients
   and process-local handles in worker composition. Resolve a registered handler
   from the saved kind/version and resume its phase with fresh dependencies.
2. **Claim work safely** — Use short `SKIP LOCKED` transactions, database-time
   leases, heartbeats and monotonic ownership tokens. Never hold a DB lock during
   external I/O. Reject expired/stale settlement and renewal.
3. **Wake and recover** — Add LISTEN/NOTIFY with ID-only payloads and indexed poll
   fallback. A dedicated listener connection handles session semantics. Sweep
   expired jobs and find due retries without relying on Redis or notifications.
4. **Settle atomically** — Commit unique output, completed job, checkpoint change
   and next job in one transaction. Stable IDs and input fingerprints distinguish
   matching retries from conflicting reuse. Keep cache projection repair durable.
   Claims, renewals, settlement and unknown-commit lookups always use the primary;
   database retry loops never repeat tool/model I/O. Order transitions for each
   affected conversation/run/group through the existing row-lock order and revision
   checks across processes. Publish accepted/saved state after commit; independent
   conversations and external handlers retain bounded concurrency.
5. **Persist retry and wait state** — Save backoff deadlines, attempt/run budgets,
   approval expiry and cancellation. Recheck access after waits. Unsafe operations
   with unknown outcomes enter reconciliation; replay requires tool idempotency
   or permission from both the saved and current replay policies. Save final
   validated arguments, version and operation identity before dispatch; reuse that
   identity for any provider idempotency lookup. A saved intent alone cannot prove
   whether an external effect happened.
6. **Bound and drain workers** — Limit per-queue and per-tenant work, reserve API
   connection capacity and stop claiming before shutdown. Preserve compatible
   worker versions for persisted work; maintenance initially shares bounded tasks
   in the worker deployment.
7. **Validate ownership and reconstruction** — Run two independent worker processes with synthetic
   handlers. Kill the claimant, drop notifications, delay completions beyond lease
   expiry, race renewals and settlements, and verify duplicate-safe logical results.
   Round-trip each supported checkpoint phase into a fresh process; prove invalid
   encoding leaves no partial writes and missing/incompatible handlers cannot run.
   Race two settlements to prove one committed result and continuation.

For example, persist phase `charge` and operation ID `payment-123` before a
synthetic payment. If its effect succeeds but result persistence fails, recovery
uses the same ID to resolve or safely retry the payment, or enters reconciliation.
The saved phase determines recovery; a vanished Python stack provides no evidence.

Completion requires one accepted job to recover across worker loss and duplicate
notifications. Model and tool orchestration remain synthetic until the next group.
