# Hosted worker scheduling, five sessions, 4 November 2026

All 100 measured turns completed correctly, with five overlapping provider calls and no
rejection, retry, throttle or unknown usage. Complete-answer p95 was 2.213 seconds, compared
with 2.963 seconds in revision 10's earlier cohort. First-text and overhead targets still miss.
[Safe measurements](2026-11-04-hosted-wake-tier-5.json) preserve all arrivals, the separate warmup,
clock bounds and 101 matched provider observations. Group 12 remains in-progress.

## Deployment and workload

Revision 12 used
`sha256:543edf85d8ed885841077771da6ff4efb8ed3f9c320281dbfdae2799d76c8c19`,
source HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes. Memory v10/system
1.2.0 are unchanged. One replica per role had 0.5 CPU/1 GiB and four pooled primary connections.
Preparation/inference concurrency was five each; summaries remain bounded to two. The existing
Sweden Central PostgreSQL 17, Redis Balanced B0 and regional gpt-4o 2024-11-20/50,000 TPM remain.

Five fresh conversations were offered every fifteen seconds, twenty turns/minute. Every answer
matched the reviewed 67-token paragraph; actual input was 1,457 tokens each. One separate warmup
preceded 100 measured turns. Token acquisition, conversation creation and clock-probe setup are
outside turn timing. All 101 calls used 147,157 input and 6,767 output tokens. The test selected
eight worker attempts per run and reserved the full possible in-flight allowance before starting.

## Results

Median and nearest-rank p95 below include all 100 measured successful turns.

| Boundary | p50 | p95 | Assessment |
| --- | ---: | ---: | --- |
| Admission | 122 ms | 180 ms | PERF-001 passed |
| Complete answer | 1.812 s | 2.213 s | PERF-003 passed |
| First visible text | 1.439 s | 1.794 s | PERF-005 missed |
| Estimated application overhead | 961 ms | 1,209 ms | PERF-002 missed |
| Terminal-write delivery lower bound | 70 ms | 105 ms | Clock-bound diagnostic |
| Terminal-write delivery upper bound | 207 ms | 351 ms | PERF-004 unqualified |
| Preparation due-to-claim upper bound | 173 ms | 330 ms | Queue target passed |
| Inference due-to-claim upper bound | 211 ms | 296 ms | Queue target passed |

Maximum due-to-claim upper bounds were 565 ms for preparation and 579 ms for inference,
below the two-second gate. These conservative bounds include claim work. Azure exact commit
timestamps remain unavailable. Terminal-write time precedes commit; its conservative upper
bound can establish compliance only when sufficiently small. The interval still includes
client/server clock uncertainty, so an upper p95 above 200 ms cannot prove a target violation.
Inspection now subtracts known server work from that uncertainty without assuming symmetric
network delay. The exported lower and upper bounds must remain together.

Overhead subtracts the single matched provider-adapter duration from complete client duration.
There are no overlapping external operations in an individual turn. It includes queues,
storage, application work and client transport; provider time includes its SDK/parser.
Against revision 10, first-text p95 fell from 2.151 to 1.794 seconds and estimated overhead p95
from 1.367 to 1.209 seconds. Different collection times and provider variation prevent assigning
all changes causally to the scheduling/read optimisations.

## Reproduction and limits

```sh
uv run python scratch/group12/run-wake-cohort.py --sessions 5 --name wake-tier-5
uv run python scratch/group12/export-wake-cohort.py wake-tier-5
```

The finite wrapper calls tracked `performance/workloads/concurrent_answers.py` with
`--worker-budget 8`, `--count 100`, `--max-output-tokens 75` and a fresh output path. It reserves
141 calls, 509,480 input and 48,535 output tokens, then charges observed complete usage.
The initial telemetry export lacked the final five calls; a later export matched all 101.
Do not rerun against existing paths or without reserving remaining authorised campaign budget.
The operator configuration is read-only; no credentials or response text are exported.

The deployed candidate passed 717 real-service tests. A later client-harness accounting fix
passed the full 719-test gate with zero skips in 141.48 seconds, without changing the image.
That fix retains a rejected subscription's slot through read-only recovery and stops new
arrivals after uncertain admission; this successful cohort encountered neither path.
The two-hour resource/idle records belong to earlier revisions. This cohort establishes no
production HA, autoscaling, workforce onboarding or new-image endurance guarantee.

## Remaining critical-path work

Matched events from the same coordinator process locate the remaining overhead. These
100-turn diagnostics exclude the external provider interval and avoid cross-host timestamp
subtraction. Provider start is reconstructed from its monotonic adapter duration and end event.

| Coordinator interval | Median | Nearest-rank p95 |
| --- | ---: | ---: |
| Preparation execution through settlement | 235 ms | 347 ms |
| Preparation settled to inference claimed | 74 ms | 141 ms |
| Inference claimed to dispatch-ready | 70 ms | 111 ms |
| Dispatch-ready to provider start | 224 ms | 312 ms |
| Provider end to inference settled | 73 ms | 110 ms |

Do not sum marginal percentiles: each row describes a different distribution. Preparation and
context/prompt construction are material parts of the path. Further work should measure their
specific transactions and reads while preserving pinned prompts, current access, lease fences,
attempt budgets and unknown-effect handling. The separate
[SQL-count diagnostic](2026-11-04-turn-sql-counts.md) provides a local structural baseline.
