# Performance register

[Requirements](REQUIREMENTS.md) define the proposed pilot targets.
[AGENTS.md](AGENTS.md) explains collection, interpretation and record updates.
The register preserves historical evidence and records the current user-authorised latency
and automatic-compaction work. Targets remain proposed engineering objectives.

## Latest recorded evidence

[Responsive compaction](records/2026-11-03-responsive-compaction.md) records the current
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
separate measurements. The proposed monitoring expansion is not implemented by this register.

[hosted-lifecycle]: ../build-order/evidence/group09/hosted-lifecycle.md
[local-scaling]: ../build-order/08-completed-recovery-and-scaling-validation.md

[semantic-memory]: ../build-order/11-completed-semantic-compaction-and-retrieval.md
