# Performance register

[Requirements](REQUIREMENTS.md) define the proposed pilot targets.
[AGENTS.md](AGENTS.md) explains collection, interpretation and record updates.
The register preserves historical evidence and records the current user-authorised latency
and automatic-compaction work. Targets remain proposed engineering objectives.

## Latest recorded evidence

[Group-12 qualification checkpoint](records/2026-11-04-group12-qualification.md) is the current
summary: 300/300 supported-tier turns, all 100 overload turns recovered, 100/100 control turns,
and 719 passing real-service tests. Revision 12 remains live. Overhead, first-text and some
final-delivery gates remain open, so group 12 stays in-progress. Campaign usage is reconciled;
257 of the approved 2,500 calls remain, fewer than a fresh three-tier qualification requires.

[Revision-12 overload](records/2026-11-04-hosted-wake-tier-10.md) accepted all 100 measured turns.
Eighty finished on their first subscription; twenty hit the stream cap and recovered correctly
through their original run IDs. All used one model call; no accepted input was lost.

[Revision-12 serial](records/2026-11-04-hosted-wake-tier-1.md) and
[two-session](records/2026-11-04-hosted-wake-tier-2.md) cohorts each passed 100/100 correct turns.
Completion p95 was 2.126/1.919 seconds. Serial final delivery passes using a 144-ms conservative
upper p95; two-session first text passes at 1.302 seconds. Other latency gaps remain recorded.

[Revision-12 five-session cohort](records/2026-11-04-hosted-wake-tier-5.md) completed
100/100 correctly. Completion p50/p95 was 1.812/2.213 seconds, first text p95 1.794 seconds.
Admission and queue targets passed; overhead/first-text targets still miss, and exact final
commit-to-delivery remains unqualified. The current full gate passed 719 tests with zero skips.

[Revision-10 five-session cohort](records/2026-11-04-hosted-read-batching.md) completed
100/100 correctly. Admission p95 was 196 ms; completion p50/p95 was 1.960/2.963 seconds.
First text and overhead still miss targets. Queue and final-delivery bounds remain unqualified.
[Mixed ordinary/compaction load](records/2026-11-04-hosted-mixed.md) completed all 406 accepted
turns, retaining two client-backlog arrivals. One-compaction completion p95 was 3.146 seconds
across 101 turns; summary generation peaked at 2.288 seconds. Actual overlap was verified in
103 ordinary turns, whose completion p95 was 2.871 seconds.

[Ownership read batching](records/2026-11-04-ownership-read-batching.md) saved a median
55 ms per paired synthetic turn with injected database delay. Fifty worker tests, including
expiry during a lock wait, and full verification passed: 708 tests with zero skips.
The [worker scheduling diagnostics](records/2026-11-04-worker-scheduling.md) also measured
completion-triggered wakeups, five preparation slots and active-memory source reuse with
injected network delay. Their combined candidate passed 717 tests with zero skips.
The [independent-process regression](records/2026-11-04-worker-wake-independent.md) recovered
all 395 accepted synthetic turns, including twenty initially rejected stream subscriptions.
Hosted qualification of this candidate remains pending.

[Full memory suite](records/2026-11-04-memory-coverage.md) passes twenty scenarios twice,
including four automatic source retrievals and all 74 expected facts. Empty-summary coverage
now remains visible to the model. Sixty directly timed summary calls took 0.52–2.53 seconds;
two incidental-summary relevance exceptions and older failed fixtures remain recorded.
Full local verification passed 707 tests with zero skips. Hosted latency gates remain open.

[Runtime read optimisations](records/2026-11-04-runtime-read-optimisations.md) remove conversation
write-lock contention from stream snapshots and batch independent claim reads. Local delayed
diagnostics saved 7–8 ms per snapshot and 44 ms per synthetic queued turn respectively;
hosted impact remains unvalidated. All 703 real-service verification tests passed.

[PostgreSQL read batching](records/2026-11-04-postgres-read-batching.md) reduced local synthetic
turn medians by 2 ms without added delay and 89 ms with injected network delay. Hosted impact
remains unvalidated; generation, ownership and lease checks remain enforced.

[Hosted concurrency](records/2026-11-04-hosted-concurrency.md) records all 400 scheduled
arrivals across 1/2/5/10 sessions. Two/five sessions completed 100/100 correctly; first text
and application overhead miss targets. Single-session and overload cohorts remain incomplete.

[Hosted monitoring](records/2026-11-04-hosted-monitoring.md) verifies four enabled Azure Monitor
alerts through explicit Fired-to-Resolved transitions using marked synthetic events.

[Hosted streaming baseline](records/2026-11-04-hosted-streaming-baseline.md) records 100
scheduled single-session arrivals: 95 correct accepted turns and five client-backlog outcomes.
Completion p95 was 2.86 seconds; first text and estimated overhead exceeded their targets.
This incomplete cohort does not qualify group 12.

[Hosted two-hour soak](records/2026-11-04-hosted-soak.md) passed 75 turns, 41 compactions
and recall after 65 minutes of passive idle/cache expiry. Resource qualification remains
partial: revision 7 lacked process RSS, task and pool-wait observations.
[Revision-8 resource samples](records/2026-11-04-hosted-resource-two-hours.md) now cover over
two hours. Pools stayed bounded and RSS changed -8.01% / -3.16% / +1.17% across the three roles.
These complement the earlier functional soak; they are separate image/workload observations.

[Ownership and increased capacity](records/2026-11-04-ownership-and-capacity.md) records a
faster metadata-only ownership check and 800 further local scheduled attempts at four/eight slots.
[Earlier memory candidate](records/2026-11-04-memory-candidate.md) records seven v9 scenarios
twice, with 28 summary calls under 5.5 seconds. Its incomplete scope and relevance/format
failures are preserved separately from the full v10 suite above.
[Live memory baseline](records/2026-11-04-memory-baseline.md) retained 13/40 attempted cases,
including relevance/format failures and a private relay reset. Full qualification remains open.
[Live memory smoke](records/2026-11-04-memory-smoke.md) passed one correction scenario twice;
three summary generations took 1.16–2.56 seconds. The full twenty-scenario gate remains pending.

[Preparation capacity](records/2026-11-04-preparation-capacity.md) compares two/five preparation
slots across 800 scheduled attempts; results do not justify increasing the hosted setting.

[Matched worker capacity](records/2026-11-04-matched-worker-capacity.md) compares one and two
independent workers at four total slots, with 800 scheduled attempts and resource/pool samples.
Completion times were similar; both configurations drained without queued pool requests.

[Local concurrent streaming](records/2026-11-04-local-concurrent-streaming.md) records 400
scheduled synthetic attempts across 1/2/5/10 sessions owned by one user. Two independent worker
processes overlapped four inference jobs; all accepted runs completed, including twenty
recovered after subscription rejection. Six serial arrivals remained client backlog.
This is local transport/execution evidence; hosted qualification remains pending.

## Earlier hosted compaction evidence

[Responsive compaction](records/2026-11-03-responsive-compaction.md) records the group-11
implementation, final 20-generation summary cohort, exploratory fidelity failures and qualified
hosted latency cohorts. All 20 final summary calls passed the checked facts/action-state checks
in 0.690–2.558 seconds. Serial response p50/p95 was 1.237/1.623 seconds; paired response p50/p95
was 1.264/1.651 seconds, with 100 correct attempts each and no throttling. PERF-001 and PERF-003
passed for the declared workload. PERF-002 overhead missed its target; PERF-004/005 are unmeasured.

## Earlier latency investigation

[Semantic-memory latency investigation](records/2026-11-03-semantic-memory-latency.md)
analyses 18 hosted turns using client measurements, Azure lifecycle logs and reconstructed
quota reservations. The deployed application limiter explains repeated 60–65-second turns;
compaction and tool-follow-up turns reached 126–130 seconds. See the
[sanitized data](records/2026-11-03-semantic-memory-latency.json) and the report's measurement
limits. These mixed correctness samples do not qualify for short-answer latency targets.

## Earlier recorded evidence

Recorded on 3 November 2026 against source commit
`69a423115c4b8a9ffa58b7bdb65805f653b0629e`:

- [Azure pilot observations](records/2026-11-03-azure-pilot.md): five individual
  hosted turns, with differing workloads; all completed.
- [Sanitised measurements](records/2026-11-03-azure-pilot.json): client timings and
  the recorded lifecycle events for the 2.509-second recall turn.
- Admission ranged from 0.158 to 1.810 seconds; complete turns ranged from 2.492
  to 11.936 seconds. These ranges mix first-use, recall, introduction and tool work.
- No comparable cohort establishes p50/p95 compliance. Model token usage and
  client-visible first-token latency were not recorded.

This is a dated register, not a live dashboard. Refresh using the procedure in AGENTS.md;
preserve historical records when the source, deployment or workload changes.

## Other evidence

- [Semantic-memory validation][semantic-memory] records a later hosted image and long-dialogue
  correctness samples with deliberate quota waits. It is a separate workload from the
  historical short conversations above; no latency-target qualification is inferred.

- [Local recovery/scaling validation][local-scaling] covers synthetic service/load measurements,
  with its own workload and limits.
- [Hosted deployment guide](../infra/azure/LIVE.md) records infrastructure, release
  timings, lifecycle validation and the current operator workflow.
- [Disposable Azure lifecycle evidence][hosted-lifecycle] covers the earlier environment's
  recovery exercises.

Local synthetic throughput, cloud recovery timings and conversational latency remain
separate measurements. Hosted monitoring currently uses application logs and four Azure Monitor
rules; distributed tracing and production paging remain future work.

[hosted-lifecycle]: ../build-order/evidence/group09/hosted-lifecycle.md
[local-scaling]: ../build-order/08-completed-recovery-and-scaling-validation.md

[semantic-memory]: ../build-order/11-completed-semantic-compaction-and-retrieval.md
