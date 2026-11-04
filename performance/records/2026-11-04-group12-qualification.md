# Group-12 qualification checkpoint, 4 November 2026

Revision 12 is hosted and healthy. All 300 realistic turns across one/two/five sessions completed
correctly. All 100 overload turns completed, including twenty recovered stream-cap rejections;
the separate 100-turn control also passed. Group 12 remains **in-progress** because overhead,
first-text and final-delivery qualification gates remain open. No target was relaxed.
[Aggregate safe evidence](2026-11-04-group12-qualification.json) links the separate populations.

## Current supported-tier measurements

Each row uses 100 comparable correct turns, plus one excluded warmup, at twenty offered
turns/minute. Responses contain 67 output tokens and 1,457 actual input tokens. Percentiles
are nearest-rank; medians are ordinary medians. Every call matched provider telemetry.

| Sessions | Admission p95 | Completion p50/p95 | First text p95 | Overhead p95 |
| --- | ---: | ---: | ---: | ---: |
| [One](2026-11-04-hosted-wake-tier-1.md) | 95 ms | 1.415/2.126 s | 1.594 s | 811 ms |
| [Two](2026-11-04-hosted-wake-tier-2.md) | 109 ms | 1.408/1.919 s | 1.302 s | 689 ms |
| [Five](2026-11-04-hosted-wake-tier-5.md) | 180 ms | 1.812/2.213 s | 1.794 s | 1,209 ms |

Admission, complete-answer and queue-wait targets pass in all three tiers. The two-session
first-text target passes; one/five exceed 1.5 seconds. All overhead p95s exceed 500 ms.
Serial final delivery is conservatively bounded at p95 144 ms, below 200 ms. Two/five upper
bounds of 230/351 ms remain inconclusive; exact Azure commit timestamps are unavailable.

The [overload report](2026-11-04-hosted-wake-tier-10.md) preserves eighty correct first
subscriptions and twenty read-only recoveries, with one model call per accepted run. Its
first-pass latency population is only eighty and does not qualify a 100-sample percentile gate.
The [two-token control](2026-11-04-hosted-wake-control-5.md) has completion p95 1.616 seconds
and first-text p95 1.459 seconds; it remains distinct from the realistic-output requirement.

## Memory, streaming and release evidence

The [mixed workload](2026-11-04-hosted-mixed.md) contains 101 one-compaction turns with completion
p95 3.146 seconds. Summary generation p95/max is 1.073/2.288 seconds. Its 103 ordinary turns
with actual summary overlap have completion p95 2.871 seconds. Two scheduled arrivals remained
client backlog after one 23.745-second provider call; all 406 accepted measured turns passed.
The [full live memory suite](2026-11-04-memory-coverage.md) passed twenty scenarios twice,
all 74 checked facts and four actual source retrievals. Relevance exceptions remain recorded.

Real local PostgreSQL/Redis verification passed **719 tests, zero skips**, in 141.48 seconds.
The deployed image passed 717 tests; two later workload-accounting regression tests bring the
source gate to 719 without changing runtime behavior or the image. Independent-process, stale
lease, restart, duplicate delivery, slow reader, cancellation and release recovery evidence
remain linked from the performance register. Four Azure Monitor alerts have firing/recovery
validation. Distributed tracing and production paging remain future work.

The staged release stopped old consumers before migration, then kept admission closed until
only healthy revision-12 replicas remained. Migration took 22 seconds; observed maintenance
was at most 648.46 seconds. The original chat remains at cursor 21 with expired Redis keys.
The previous cancelled-unknown outcome is preserved. Azure remains running; no data service was
replaced. Terraform has no infrastructure drift, and its restored API URL output was refreshed.

## Remaining performance work and bounds

[Matched worker stages](2026-11-04-hosted-wake-tier-5.md) locate material overhead in preparation
and context/prompt construction. [Passive diagnostics](2026-11-04-hosted-wake-diagnostics.md)
show transient pool contention, small sampled CPU throttling and wider clock uncertainty on
cold HTTP connections. The next experiment should isolate transaction/read and pool behavior,
then explicitly warm clock-probe connections before calibration. Historical results stay intact.
No larger pool, CPU allocation or new cloud resource was configured from these diagnostics.

The complete campaign, including retained unknown-usage reservations, is charged as follows:

| Bound | Approved cap | Charged usage | Remaining |
| --- | ---: | ---: | ---: |
| Model calls | 2,500 | 2,243 | 257 |
| Input tokens | 7,000,000 | 5,524,353 | 1,475,647 |
| Output tokens | 450,000 | 97,406 | 352,594 |

All current allocations reconciled; no inference campaign or exec helper remains running.
A fresh one/two/five-session qualification requires 303 expected model calls before additional
in-flight reservation headroom, exceeding the remaining 257-call allowance. Further complete
hosted qualification therefore needs a new bounded allocation. Existing resources stay alive.
The two-hour functional and resource records belong to earlier revisions; the current image
has approximately 27.5 minutes of resource samples, with bounded pools/tasks and modest RSS change.

Reproduction commands are in each linked record and [the operator procedure](../AGENTS.md).
Raw local artifacts remain under ignored scratch/group12. The local build-order directory stays
ignored and in-progress. JSON/line-length checks and git diff --check pass. Source was
uncommitted when measurements were collected; the image digest identifies the hosted build.
