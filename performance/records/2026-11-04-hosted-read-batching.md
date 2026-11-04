# Hosted read batching, 4 November 2026

The revision-10 five-session cohort completed 100/100 measured turns correctly, with five
observed overlapping provider calls and no rejection, retry, throttle or unknown usage.
Admission and complete-answer timing passed their targets. First text and application overhead
still miss; group 12 remains in-progress. [Safe observations](2026-11-04-hosted-read-batching.json)
retain every arrival, the separate warmup and correlated worker spans.

## Deployment and workload

The immutable image was
`sha256:94f2a1ead6269dea8131e5ba4715fde53c356b08638b805eec65d828e08db0e9`,
with one API/coordinator/tool replica, each 0.5 CPU/1 GiB, four primary connections per process,
two preparation slots and five inference slots. PostgreSQL 17 is a private D2ds v5 single
primary; Redis is Balanced B0. Foundry used gpt-4o 2024-11-20, regional Standard, 50,000 TPM.
Source was HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes.
This image contains batched generation/head and claim reads, the nonblocking stream snapshot,
and memory v10/system 1.2.0. Later local ownership batching is excluded.

The reference client offered five fresh conversations every fifteen seconds: 100 measured
turns at twenty/minute, plus one warmup. Each request used 1,457 actual input tokens and the
same reviewed 67-token answer. Client token acquisition and conversation creation were outside
timing. The measurement ran from 16:20 UTC for 288.39 seconds; achieved throughput over that
finite start-to-final interval was 20.81/minute. This does not establish saturation capacity.
All 101 calls consumed 147,157 input and 6,767 output tokens.

## Results and boundaries

Median and nearest-rank p95 below use all 100 correct measured turns, excluding only warmup.

| Boundary | p50 | p95 | Target assessment |
| --- | ---: | ---: | --- |
| Admission | 129 ms | 196 ms | PERF-001 passed |
| Complete answer | 1.960 s | 2.963 s | PERF-003 passed |
| First visible text | 1.515 s | 2.151 s | PERF-005 missed |
| Estimated application overhead | 971 ms | 1,367 ms | PERF-002 missed |
| Terminal-write to delivery upper bound | 226 ms | 279 ms | PERF-004 unqualified |
| Preparation due-to-claim upper bound | 340 ms | 672 ms | 500-ms queue target unqualified |
| Inference due-to-claim upper bound | 165 ms | 248 ms | Within queue target |

Overhead subtracts the one observed provider-adapter call from each complete client duration;
there are no parallel external operations in this cohort. It includes client transport.
The provider call includes SDK transport/parsing. No prompt, answer or credential is exported.
Azure exact commit timestamps remain unavailable. Terminal-write intervals bracket client/server
clock alignment and begin before commit; their upper bounds cannot establish PERF-004 compliance
when above 200 ms. Due-to-claim upper bounds include claim work and may include ineligible time;
preparation therefore still needs improvement and more precise eligibility attribution.

Against revision 8's earlier five-session cohort, final median improved from 2.114 to 1.960 s,
while p95 changed from 2.811 to 2.963 s. First-text p95 improved from 2.408 to 2.151 s; estimated
overhead p95 changed from 1.391 to 1.367 s. These are separately collected hosted cohorts with
provider variation, so they do not isolate a causal speedup from database batching.

## Reproduction and limits

The authorised wrapper and exact invocation remain in ignored scratch:

```sh
uv run python scratch/group12/run-coverage-tier5.py
uv run python scratch/group12/export-coverage-concurrency.py
```

The wrapper uses `performance/workloads/concurrent_answers.py`, a read-only operator config,
a fresh output path, a finite allocation and campaign reconciliation. Reconcile remaining caps
before any rerun; never overwrite the raw cohort or run alongside another model experiment.
The exporter waits for Log Analytics and matches run/job/attempt IDs. All 100 measured provider
calls and both worker claims were present. Raw metadata and queries remain under
`scratch/group12/coverage-tier-5*`; synthetic response review occurred before export.

One/two/ten-session and two-token control qualification for this image remains unexecuted.
The earlier cohorts retain every failure/backlog outcome. This five-session result does not
replace the separate two-hour soak, mixed compaction workload or workforce onboarding evidence.
