# Performance register

[Requirements](REQUIREMENTS.md) define the proposed pilot targets.
[AGENTS.md](AGENTS.md) explains collection, interpretation and record updates.
This branch establishes the register and preserves existing evidence. Optimisation,
new instrumentation and a percentile benchmark belong to subsequent work.

## Latest recorded evidence

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
