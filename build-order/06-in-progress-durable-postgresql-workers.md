# Durable PostgreSQL workers

Status: in-progress. Depends on PostgreSQL records and Redis recovery semantics.
The implemented scope and validation evidence below are retained. Additional
worker tasks 8–11 below define the remaining work. They are planned; the existing
implementation evidence covers tasks 1–7.

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
The group also requires the remaining worker tasks and their acceptance gates below.

## Remaining worker tasks

8. **Expose failure and recovery reasons** — Extend the existing persisted budgets
   and reconciliation with bounded reason codes and allowed recovery actions for
   unavailable handler versions, exhausted attempts, repeated process crashes and
   uncertain effects. Expose tenant-scoped inspection through trusted application
   interfaces with current access checks. Return safe status and operation identity;
   raw errors, credentials and restricted payloads remain outside these responses.
9. **Bound repeated crashes** — Use durable attempt/run allowances to stop repeated
   dispatch after process crashes, including crashes before an outcome is saved.
   Preserve an inspectable reason when the allowance is exhausted. Orderly draining
   must not count as an execution failure. Unavailable handlers and stopped work
   must not retain execution capacity or prevent unrelated eligible jobs progressing.
10. **Add authorised recovery controls** — Define eligible inspection, retry and
    reconciliation actions against the saved reason and current state. Recheck
    current access, approvals, handler versions and replay policy. Preserve the
    operation identity and committed outcomes, and durably record actor, decision
    and request identity. Matching control retries are idempotent; conflicting
    reuse rejects. Never bypass uncertain-effect reconciliation or silently reset
    exhausted budgets. Keep these controls in the worker/application boundary.
11. **Validate recovery controls with synthetic handlers** — Repeatedly kill one
    handler across fresh processes until its durable allowance stops dispatch;
    verify another tenant's eligible jobs still progress. Compare orderly drain
    with crashes and prove unavailable/stopped work holds no execution lease.
    Exercise authorised, denied, duplicate, conflicting and stale control requests,
    lost commit acknowledgements, revoked approvals and changed replay policy.
    Inspection must retain safe reasons and unknown operation identities without
    restricted data. Recovery must neither repeat a committed effect nor authorise
    an unsafe unknown effect. Record executed evidence separately from tasks 1–7.

## Implementation and acceptance evidence

Implemented in `src/bankeriq_agent_platform/workers/`, with migrations 008/009 and
shared admission/checkpoint transaction builders in the PostgreSQL store. The
[worker guide](../src/bankeriq_agent_platform/workers/README.md) records composition,
reproduction commands, control semantics and operational limits.

The implementation keeps one enrolled synthetic checkpoint task active per run.
Its task kind identifies the operation; the versioned recovery phase records ready,
dispatch, retry, approval, reconciliation or completion. Real coordinator/tool batches
and group joins remain group 07. Existing `StateStore`, in-memory execution,
coordinator, continuations and `open_runtime()` retain their existing profile.

Acceptance coverage in `tests/integration/test_workers.py`:

1. Strict JSON rejects unsupported values, non-finite numbers, non-string keys,
   unknown fields/versions and incompatible handlers. Every supported phase is
   reconstructed from PostgreSQL in a fresh interpreter with fresh dependencies.
   Invalid admission encoding leaves no input/job/run writes.
2. Independent processes claim using database-time leases and monotonic tokens.
   Stale owners cannot renew or settle; renewal/settlement races commit one result.
   Expiry during settlement rolls back output, checkpoint and continuation.
3. The claimant is killed after dispatch preparation. A second process recovers
   through indexed polling after duplicate notifications were sent with no listener.
   Exactly one saved result, transition and ready continuation remain. Dedicated
   listener tests verify ID-only payloads, timeout and reconnection.
4. Failure injection after output, next-task creation and final settlement rolls
   the transaction back. Unknown claim, settlement, approval, wait and reconciliation
   commits resolve stable identities. A lost database acknowledgement never invokes
   the synthetic external handler twice. Two independent settlement processes return
   one committed checkpoint and continuation. Conflicting operation IDs roll back
   admission; nonterminal settlement requires recoverable next work.
5. Retry deadlines, attempt/run budgets, exact approval binding, approval expiry,
   cancellation and current access checks are exercised. A process killed after a
   synthetic unsafe effect leaves its operation ID in a separate provider record;
   a fresh process resolves that identity and commits its outcome without replay.
   Revoking replay permission parks work for reconciliation. Cancellation retains
   unknown dispatched effects explicitly and fences late completion.
6. Six independent claim processes exercise global queue and per-queue tenant limits.
   Locked conversations do not block independent progress. Connection sizing rejects
   configurations that consume the API reserve. Heartbeats keep a slow handler owned
   during drain; shutdown leaves the next task unclaimed. Bounded maintenance repairs
   Redis projection through the existing session service.
7. Restricted synthetic output passes through existing output enforcement. Private
   bytes remain absent from jobs, journal, checkpoints, results and transitions;
   opaque delivery metadata remains separate from model history. Redis deletion
   reconstructs accepted/saved history from PostgreSQL. Timeline and deletion checks
   fence recovered owners; wrong-user access fails.

## Executed validation — 3 November 2026

Local disposable Docker services: PostgreSQL 17.11 and Redis 7.4.12. Tests use isolated
PostgreSQL schemas and a disposable Redis database. No real model/tool calls, cloud
provisioning or deployment were performed.

```sh
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55436/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56386/0'
uv run python scripts/verify.py
git diff --check
```

Verification passed with 371 tests, including 47 worker acceptance cases and no
service-test skips. Lockfile, formatting, lint, readability, JSON style, the
100-character line limit and type checks passed. `git diff --check` and changed
documentation file-link checks passed. This evidence predates the subsequent
user-authorised commit and push.

## Remaining limits

All group-06 tests are local synthetic evidence. Production identity, HA failover,
backup restoration, throughput/latency SLOs, real asynchronous coordinator/tool
orchestration, semantic compaction and cloud deployment remain later groups.
Deployment must enforce the configured worker process ceiling for connection sizing;
this group does not configure production database roles or a cloud replica limit.
Unknown effects with no compatible lookup or exhausted reconciliation budget remain
explicitly unresolved for operator/provider review. Cancellation does not undo an
external effect. These cases never authorize automatic unsafe replay.
