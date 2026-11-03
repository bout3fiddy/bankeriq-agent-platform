# Durability design references

The planned persistence, recovery and handoff groups incorporate source review
of Pi's durable package at commit `95485d32f19d5ab9dca81bf2d7299661347d3238`
on 2 November 2026. Pi's tests were inspected, not executed. These are proposed
BankerIQ adaptations; only groups marked implemented have execution evidence.

Serialization follow-up reviewed on 3 November 2026 against the local Pi checkout
at commit `f9daca3480732d0042b1df3a6e27221f597a620d`. This adds planned requirements
to Redis history, durable workers, asynchronous execution and recovery validation;
completed storage milestones retain their existing evidence. Pi tests were read
only. No Pi dependency or Chord integration is proposed.

- **Serialized recovery state**
  Persist strict JSON phase records and reconstruct handlers from registered
  definitions. Keep executable objects and credentials outside storage. Extend
  [the existing codec](../src/bankeriq_agent_platform/storage/codec.py) within its
  permitted variants and bounds. [Pi JSON ownership][pi-json-source] and
  [phase recovery][pi-phase-source] inform worker steps 1 and 7.

- **Serialized commits**
  Pi queues Session mutations through storage settlement and adoption. BankerIQ
  applies its existing PostgreSQL locks, revisions and ownership checks across
  workers; one process-local queue cannot establish that guarantee. See worker
  step 4 and [Pi's Session mutation boundary][pi-commit-source].

- **Incremental state and disposable progress**
  Pi stores document deltas with periodic bases and permits deferred persistence
  for model partials/tool progress. BankerIQ uses bounded journal snapshots and
  Redis progress, with accepted execution state committed to PostgreSQL. See Redis
  step 3, asynchronous execution step 6, [Pi checkpoints][pi-checkpoint-source]
  and [deferred persistence][pi-progress-source].

- **Intent and replay policy**
  Planned adaptation and source: Persist per-call intent and require saved/current policy to permit
  recovery. [Pi tool recovery][line-source-1].

- **Atomic settlement**
  Planned adaptation and source: Commit conversation results, deduplication and the next queued job
  atomically in PostgreSQL. [Pi settlement][line-source-2].

- **Persistent retry state**
  Planned adaptation and source: Preserve deadlines and restart counts; keep orderly shutdown
  distinct. [Pi task states][line-source-3].

- **Durable identity**
  Planned adaptation and source: Deduplicate admission and bind IDs to matching intent. Pi's
  top-level request check is weaker than its nested-call check; use argument/version matching for
  BankerIQ. [Pi request admission][line-source-4], [nested-call identity][line-source-5].

- **Versioned checkpoints**
  Planned adaptation and source: Reject incompatible persisted work initially; introduce explicit
  migrations when needed. [Pi definitions][line-source-6], [compatibility tests][line-source-7].


Pi assumes [one process owns storage][line-source-8].
Its startup recovery resets running tasks to pending. BankerIQ needs cross-process
leases, ownership tokens and revision checks before reclaiming jobs. Existing
`StateStore`, coordinator and continuation boundaries remain the starting point;
adopting Pi's single-process scheduler is outside this plan.

## Commit-history follow-up — 3 November 2026

Reviewed Pi history through `f9daca3480732d0042b1df3a6e27221f597a620d`; tests and
reported benchmarks were inspected without rerunning them. The following are
planned BankerIQ requirements, with acceptance evidence owned by each group:

- [Group 06](06-in-progress-durable-postgresql-workers.md#remaining-worker-tasks)
  owns inspectable failure reasons, bounded crash recovery and authorised operator
  controls, informed by [Pi's crash-loop corrections][pi-crash-history]. Existing
  worker evidence remains separate from these planned follow-up acceptance gates.
- [Group 07](07-planned-asynchronous-coordinator-and-tools.md) adds explicit child
  ownership, integration with worker recovery and slow-consumer bounds, informed
  by Pi's [ownership changes][pi-ownership-history] and
  [folded watch updates][pi-watch-history]. PostgreSQL remains the authority
  for jobs; Redis and bounded per-connection buffers carry disposable progress.
- [Group 08](08-planned-recovery-and-scaling-validation.md) adds long-lived worker
  and growing-history workloads, informed by [scheduler indexing][pi-scheduler-history]
  and [incremental parsing][pi-stream-history]. Pi's reported timings are not
  BankerIQ performance evidence or targets.
- [Group 10](10-planned-reviewed-composite-business-tools.md) rejects cyclic
  dependencies in reviewed compositions, informed by [Pi's wait-cycle fix][pi-cycle-history].
  Model-written execution graphs remain conditional work.
- [Group 11](11-planned-semantic-compaction-and-retrieval.md) budgets summarization
  itself and makes required-compaction failure stop dispatch, informed by
  [oversized summary inputs][pi-summary-history] and [failure handling][pi-compaction-history].

## Infrastructure references

[PostgreSQL queue locking](https://www.postgresql.org/docs/current/sql-select.html)
and [LISTEN/NOTIFY semantics](https://www.postgresql.org/docs/current/sql-notify.html)
inform worker claims and wake-ups. The
[Redis persistence documentation](https://learn.microsoft.com/en-us/azure/redis/how-to-persistence)
explains why idle-only flushing cannot guarantee recovery of accepted turns.
[Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app)
and the [KEDA PostgreSQL scaler](https://keda.sh/docs/2.21/scalers/postgresql/)
inform deployment validation; availability in the actual host remains a gate.

[line-source-1]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L181-L226
[line-source-2]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L828-L872
[line-source-3]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/types.ts#L657-L746
[line-source-4]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/submissions.ts#L211-L240
[line-source-5]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/harness/tool.ts#L898-L946
[line-source-6]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/src/types.ts#L364-L385
[line-source-7]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/test/harness-tasks-recovery.test.ts#L448-L525
[line-source-8]:
  https://github.com/earendil-works/pi/blob/95485d32f19d5ab9dca81bf2d7299661347d3238/packages/durable/README.md#L854-L883

[pi-json-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1518-L1549
[pi-phase-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L2038-L2163
[pi-commit-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/src/session/session.ts#L155-L171
[pi-checkpoint-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1562-L1613
[pi-progress-source]:
  https://github.com/earendil-works/pi/blob/f9daca3480732d0042b1df3a6e27221f597a620d/packages/durable/docs/spec.md#L1834-L1852

[pi-ownership-history]:
  https://github.com/earendil-works/pi/commit/03180653c624d1c079a509c61ab30322e7ff7b62
[pi-crash-history]:
  https://github.com/earendil-works/pi/commit/440a5fcdb8b2a204429c280109b2dbf6b123ef35
[pi-watch-history]:
  https://github.com/earendil-works/pi/commit/d8d99950ac360bc9330245030f587b32988314fb
[pi-scheduler-history]:
  https://github.com/earendil-works/pi/commit/cb437549318e06de1c3b00786f84236a0a4c896e
[pi-stream-history]:
  https://github.com/earendil-works/pi/commit/f715a15c101ca8a44d759bceeb7ec2a51024c81c
[pi-cycle-history]:
  https://github.com/earendil-works/pi/commit/eb2fce43d22cb41771c4538e0c9f5e9614e5e2a1
[pi-summary-history]:
  https://github.com/earendil-works/pi/commit/15e7bdc14cae38786c4cde24870ffce888e062cd
[pi-compaction-history]:
  https://github.com/earendil-works/pi/commit/89e5c2c075535e2f427c30fc74720d65b2944ff9
