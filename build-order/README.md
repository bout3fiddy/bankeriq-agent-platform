# BankerIQ build order

Build from core runtime logic towards external integrations. These steps are
planned except where marked completed. Keep external I/O mocked until the adapter steps.

Work one component at a time. Each step includes its implementation, meaningful
tests and supporting documentation before the next step begins. Reuse the existing
application composition and keep unfinished integrations explicitly labelled.

Task groups run in this order: initial runtime → containerisation → PostgreSQL
journal and job records → shared system prompts → Redis sessions and asynchronous
history → durable workers → asynchronous coordinator and tools → recovery and
scaling validation → Azure runtime deployment. Each group has its own task
numbering. Durable PostgreSQL workers remain in progress for additional tasks.

The selected stack is PostgreSQL plus ephemeral Redis. PostgreSQL contains durable
conversation deltas, history snapshots and the message queue. Redis serves active
UI views through the API; large history writes happen asynchronously and on idle.
Accepted work always has a recoverable PostgreSQL input record. See the
[full system design](../ARCHITECTURE.md#selected-state-and-execution-services).
The transaction and replication
[boundaries](../ARCHITECTURE.md#transaction-and-replication-boundaries)
define atomic writes, primary-only reads and recovery requirements for the groups
below. The PostgreSQL journal adapter and its local failure tests are implemented;
synthetic worker ownership and recovery are now validated locally.
Real asynchronous orchestration and deployment remain pending.
DTS and separate brokers are outside this sequence. Tools own confidential Blob
reports and authenticated downloads. Container Apps is proposed for worker hosting;
the earlier Foundry Hosted Agents target remains a separate compatibility decision.

## Navigation

Read [AGENTS.md](AGENTS.md) for selection, execution and maintenance guidance.
Each numbered file owns one task group, its status, dependencies, acceptance gates
and evidence. Numbers preserve the existing reading order; dependencies inside each
file determine execution eligibility. Follow-up groups remain after infrastructure.

- 01 — Initial agent runtime
  [completed](01-completed-initial-agent-runtime.md)
- 02 — Containerisation
  [completed](02-completed-containerisation.md)
- 03 — PostgreSQL journal and job records
  [completed](03-completed-postgresql-journal-and-job-records.md)
- 04 — Shared system prompts
  [completed](04-completed-shared-system-prompts.md)
- 05 — Redis sessions and asynchronous history
  [completed](05-completed-redis-sessions-and-history.md)
- 06 — Durable PostgreSQL workers
  [in-progress](06-in-progress-durable-postgresql-workers.md)
- 07 — Asynchronous coordinator and tool queues
  [planned](07-planned-asynchronous-coordinator-and-tools.md)
- 08 — Recovery and horizontal scaling validation
  [planned](08-planned-recovery-and-scaling-validation.md)
- 09 — Azure runtime deployment
  [planned](09-planned-azure-runtime-deployment.md)
- 10 — Reviewed composite business tools
  [planned](10-planned-reviewed-composite-business-tools.md)
- 11 — Semantic compaction and history retrieval
  [planned](11-planned-semantic-compaction-and-retrieval.md)
- 12 — Follow-up runtime work
  [planned](12-planned-follow-up-runtime-work.md)

Current: [durable PostgreSQL workers](06-in-progress-durable-postgresql-workers.md).

Use exactly three statuses: `planned`, `in-progress` and `completed`.
`planned` means work has not started. Mark a group `in-progress` as soon as work
starts and keep it there until all required work is finished. Mark it `completed`
when its requirements, tests, documentation and acceptance gates are satisfied.
Keep filenames, Status paragraphs and index entries aligned. See
[AGENTS.md](AGENTS.md#status-and-evidence-maintenance) for maintenance guidance.

## Scope boundary

Real tool applications, durable workers, persistent storage, enterprise identity
integration and the browser app remain outside the completed initial core scope.
Tools own confidential Blob reports, authenticated download endpoints and report
lifecycle. The runtime forwards approved delivery links for the user to open and
verify; it has no report-read access. Tool storage is outside this build sequence.
Permission checks and output enforcement are real runtime logic; their external
providers and execution dependencies are mocked. The self-hosted specialist is
deferred.


## Design sources

[Durability design references](design-references.md) retain the reviewed external
sources and their provenance. They explain planned adaptations and do not establish
executed validation. Group files contain the implementation evidence.
