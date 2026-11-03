# Recovery and horizontal scaling validation

Status: planned. Depends on asynchronous coordinator and tools.

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
[worker recovery gates](06-in-progress-durable-postgresql-workers.md#remaining-worker-tasks)
and the ownership/slow-client gates from
[asynchronous execution](07-planned-asynchronous-coordinator-and-tools.md).
