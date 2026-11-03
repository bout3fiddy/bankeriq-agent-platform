# Storage lifecycle and capacity controls

Status: planned. Part of Operational reliability and capacity management. Depends on
PostgreSQL journal/job records, Redis sessions/history and asynchronous coordinator/tool
execution (groups 03, 05–07). Coordinate load measurements and recovery scenarios with
[group 08](08-completed-recovery-and-scaling-validation.md) and the concurrent-session
measurements in [group 12](12-planned-concurrent-sessions-streaming-and-memory-quality.md).
Renumbered from 13 to 14 on 4 November 2026 to make room for workforce onboarding.
This item does not authorise implementation, deployment or cloud provisioning.

## Purpose and current boundary

Bound retained infrastructure state as conversations and completed jobs accumulate.
Individual record limits, bounded reads and history snapshots do not establish a total
storage bound. Snapshots do not by themselves permit deletion of the underlying journal.
Retention periods, quotas, retry/reconnect windows and capacity thresholds require explicit
policy decisions during this group's implementation; none are selected by this plan.

## Planned requirements

1. **Define retention and recovery windows** — Inventory conversation events, snapshots,
   history pages, runs, jobs, results, approvals, prompt manifests/versions, semantic summary
   versions, compaction attempts, retrieval receipts, deduplication records and audit evidence.
   Include local identity/membership records and access audits introduced by
   [group 13](13-planned-workforce-identity-and-access-onboarding.md), when available.
   Define retention by record class and tenant policy, including
   deletion holds and dependencies. Specify supported retry, reconnect and recovery windows
   and the response to requests older than those windows. Retain referenced prompt versions
   and the minimum identity evidence needed to prevent old requests executing again.
2. **Implement bounded lifecycle cleanup** — Expire eligible completed conversations and
   execution records in small, restartable batches. Preserve active work, unresolved unknown
   effects, approvals, required audit evidence and deletion/recovery fences. Coordinate
   journal pruning with durable snapshots, history reads, cursors and projection progress.
   Include [semantic memory](11-completed-semantic-compaction-and-retrieval.md) provenance
   and retrieval windows; a semantic summary alone does not permit source-journal deletion.
   Fence late workers and cache refills so cleanup cannot resurrect deleted state. Expose
   dry-run eligibility and counts before enabling destructive cleanup under an approved policy.
3. **Enforce tenant quotas and admission limits** — Measure retained usage and define limits
   for accepted input, conversation growth and outstanding work. Enforce concurrent admissions
   consistently across API replicas. Reserve capacity for already accepted jobs, settlement
   and recovery, so reaching a quota cannot strand work. Define safe rejection and retry
   responses and repair accounting after cleanup, migration or restore.
4. **Monitor capacity and growth** — Track table/index sizes, retained bytes by record class
   and tenant, cleanup backlog/age, growth rate and free-space headroom. Account separately
   for WAL, vacuum/bloat, replication slots and backups. Establish alert thresholds and
   capacity forecasts from measured workloads; record operator actions for low-space events.
5. **Plan physical storage and archival** — Evaluate partitioning and archival of large event
   and job tables against measured growth and query plans. Require archive integrity and
   recovery/access tests before deleting source data. Verify maintenance and vacuum behavior;
   logical deletion alone does not guarantee immediate filesystem space reclamation.
   Consider larger primary capacity before write sharding. Add read replicas only for
   measured lag-tolerant demand; they do not increase write capacity. Tenant sharding remains
   conditional work within this group, as specified below.
6. **Coordinate recovery and operations** — Define cleanup scheduling, concurrency, permissions
   and safe audit records. Exercise quota/accounting and retention behavior after restore,
   recovery-generation changes and Redis loss. Preserve reconciliation evidence for external
   effects. Document how database, backup and any archive retention interact. Confidential
   tool-owned reports retain their separate storage and lifecycle ownership.

## Conditional capacity option: tenant sharding

Implement trusted tenant routing only after measured write bottlenecks justify it. Co-locate
all tenant conversation, queue, checkpoint and deduplication records. Test fenced shard moves,
recovery-generation changes and prompt resolution from the authoritative configuration writer.
Splitting one tenant needs a separate design. Preserve atomic local transitions and reject
stale routing/owners; do not assume cross-shard transactions. Record the measured need and
an explicit implementation decision before enabling this option. Without that evidence,
complete the storage lifecycle gates on the existing primary and leave sharding planned.

## Acceptance gates

- Real local PostgreSQL/Redis tests show bounded cleanup batches, repeatable restart and
  tenant isolation while workers and admissions continue concurrently.
- Retention eligibility tests prove active work, unknown effects, required prompt versions
  and deduplication/recovery evidence survive until policy permits removal. Delayed workers,
  projections and old request IDs cannot recreate deleted state or repeat an external effect.
- Concurrent quota tests demonstrate consistent admission decisions, correct accounting after
  retries/cleanup and reserved headroom for accepted work to settle or reconcile.
- Reconnect/history behavior is explicit on both sides of the retention boundary. Snapshot,
  journal, archive and backup recovery tests match the documented supported windows.
- A sustained workload with fixed policies reaches a measured steady retained footprint for
  eligible data after cleanup catches up. Account separately for protected unresolved records;
  their growth must trigger operational action. Record table/index/WAL sizes and query plans.
- Simulated capacity thresholds and cleanup failures trigger the documented alerts and safe
  admission behavior. Record maintenance impact, batch limits and remaining operational gaps.

## Evidence

Planning only. No lifecycle cleanup, quota enforcement, archival or capacity monitoring is
implemented or validated by this item. Record reproducible commands and executed evidence
here when implementation begins; skipped service tests cannot satisfy these gates.
