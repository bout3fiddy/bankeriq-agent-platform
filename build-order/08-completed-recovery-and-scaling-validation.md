# Recovery and horizontal scaling validation

Status: completed. Depends on asynchronous coordinator and tools.

1. **Exercise crash boundaries** — Kill processes after admission, claim, model
   response, tool effect, result commit, join and final persistence. Cover ambiguous
   side effects and unknown commit outcomes without automatic unsafe replay.
   Verify each atomic-write group leaves either its complete transition or no
   transition after rollback, including cancellation racing with settlement.
2. **Test migrations and retention** — Run mixed compatible worker versions, reject
   unsupported checkpoints, preserve old state on migration failure, and verify
   retention cannot remove deduplication/recovery evidence early. Deletion cannot
   be undone by a delayed projection or worker. Include strict-JSON rejection,
   encode/decode round trips and restart from each phase. Exercise every saved/current
   replay-policy combination, including a policy revoked between attempts.
3. **Measure concurrency** — Use multiple API, coordinator and tool processes.
   Preserve one active logical run per conversation, fairness between tenants and
   bounded model requests. Measure queue age, commit latency, warm/cold reads,
   first-token latency, projection lag and DB resource usage under stated load.
4. **Tune budgets and scale signals** — Set lease/heartbeat, batch, idle and replica
   limits from evidence. Scale on eligible backlog, excluding sleeping retries and
   approval waits. Verify backpressure, bounded caches and connection-pool limits.
5. **Validate recovery operations** — Reconstruct after full Redis loss, exercise
   PostgreSQL unavailability and drain workers. On primary reconnection, resolve
   outstanding IDs before retrying. Restore a database backup with dispatch paused;
   fence old workers, advance the recovery generation, rebuild Redis and reconcile
   effects and acknowledged requests beyond the restored point before resuming.
6. **Validate read routing** — Check the initial adapter uses the primary for all
   database operations. Claims, deduplication, prompt activation reads and
   cursor-required recovery retain this route when optional replica reads arrive.
   Require a local controlled-lag test before enabling replica history/reporting.
   Azure HA failover is a separate hosted gate.
7. **Exercise long-lived workers and growing history** — Hold active concurrency
   and payload sizes fixed while completing thousands of jobs across reused and
   new conversations. Separately grow retained history and stream length. Record
   process memory after idle cleanup, retained task/cache entries, claim-query
   plans and rows visited, database reads, CPU and bytes written per progress
   update. Compare early and late workload windows under the same configuration.
   Require bounded retained memory after cleanup and indexed claims independent
   of completed-job scans. Ordinary progress must not repeatedly parse, copy or
   load the full growing transcript. State workload-specific regression limits
   before accepting results; retain measurements and query plans as evidence.

Completion requires executed failure and load evidence with explicit limits.
Horizontal worker capacity remains bounded by PostgreSQL and model/tool quotas.
Include the long-lived workload above, the
[worker recovery gates](06-completed-durable-postgresql-workers.md#worker-recovery-extensions)
and the ownership/slow-client gates from
[asynchronous execution](07-completed-asynchronous-coordinator-and-tools.md).

## Implementation and executed evidence

Implemented on 3 November 2026. Migration 012 adds partial eligible-work/resource-holder
indexes and an ordered child-status index. Progress uses a separate bounded Redis
key; ordinary token updates and SSE metadata polls avoid checkpoint/transcript
reconstruction. The PostgreSQL adapter supports an optional bounded primary connection
pool with explicit shutdown. Workers survive failed claim connections and retry on
their bounded poll interval. `workers.scaling.pressure()` reports compatible due work,
queue age and leases while excluding sleeps, approvals, unavailable lookups, unsafe
replay, exhausted budgets and blocked resources/tenant capacity.

The [reproduction guide and raw evidence](evidence/group08/README.md) record service
versions, commands, declared regression limits, initial failures and remaining limits.
Verification used real local PostgreSQL/Redis and independent processes. No skipped
service tests contribute to acceptance.

| Gate | Executed evidence |
| --- | --- |
| Process loss | Seven kill/restart boundaries, from admission through final persistence |
| Atomicity and races | Existing rollback, unknown-commit, cancellation/fan-out/settlement gates |
| Compatibility | Concurrent version-1/version-2 processes; unavailable version-3 work stays ready |
| Replay policy | All 16 saved/current policy combinations after persisted dispatch and restart |
| Schema and JSON | Failed migration rollback, old-state reads, strict JSON/version rejection |
| Cleanup and isolation | Durable dedup/reconciliation retained; deletion/output/control gates |
| Capacity | Six API/coordinator/tool processes, 400 turns, 2,800 jobs, 400 unique joins |
| Outcomes | 1,200 distinct tool results, zero remaining leases or execution tasks |
| Recovery | Actual PostgreSQL stop/start and pg_dump/pg_restore with generation fencing |
| Read routing | State and prompt clients reject read-only sessions; replica route disabled |
| Long-lived state | Per-process RSS after drain below early samples; bounded pool/cache entries |
| Query cost | Partial index used after 2,800 jobs; zero completed rows scanned |

The final load completed in 43.4 seconds. Admission including durable commit was
11.7 ms median/17.0 ms p95. First-token latency was 3.79 seconds median/34.42 seconds
p95, including queued turns on reused conversations. Warm/cold session reads were
2.21/9.75 ms median. Peak measured connections were 15, projection lag 73 cursors and
eligible queue age 34.0 seconds; total WAL was 42,366,408 bytes. Both tenants progressed
in early and late windows. All six processes retained less RSS after drain than at
the early sample, with zero queued pool borrowers. The post-load candidate plan used
`worker_eligible_scan`, visited zero rows, hit two shared buffers and read none from disk.

At 3,000 retained turns, the matched progress probe improved median latency from
243.9 to 19.1 ms and eliminated two history reconstruction calls per update. Separate
pooled I/O measurements at 10 and 3,000 turns both recorded 11 SELECTs and 324 WAL bytes
per progress update, with 32 retained Redis entries. The workload limits passed.

The restore rehearsal paused dispatch, restored a real logical backup into a fresh
database, advanced the recovery generation and rejected an old owner's renewal.
An external receipt resolved one unknown effect with zero re-executions. The original
request ID recovered a post-backup acknowledged request without duplication, and
Redis was rebuilt on the new generation. A separate independent worker survived an
actual primary stop/start, accepted no work during the outage and drained after
same-ID recovery; restart-to-completion took 2.17 seconds.

Final quality gate: `uv run python scripts/verify.py` passed all static checks and
**458 tests in 57.60 seconds, with no skips** against the local services.
`git diff --check` passed. The synchronous in-memory profile remains included in this
suite; no cloud resources were provisioned or deployed.

## Remaining limits

These measurements validate the stated synthetic local workload. They establish no
production throughput SLO, cloud failover guarantee or unlimited horizontal scaling.
The restore receipt/acceptance ledgers are synthetic; production cutover requires
actual durable provider receipts and acknowledged-request reconciliation. Optional
replica history/reporting remains disabled until controlled-lag tests exist. Azure HA
and hosted shutdown remain group 09 gates. Storage retention, quotas and growth alerts
remain planned in [group 14](14-planned-storage-lifecycle-and-capacity-controls.md);
current cleanup preserves all durable recovery evidence.
