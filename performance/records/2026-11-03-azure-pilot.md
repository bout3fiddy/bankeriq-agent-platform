# Azure pilot observations: 3 November 2026

Evidence type: individual observations. These five turns used different requests in one
conversation and establish no p50/p95 threshold compliance. Optimisation remains deferred.
The accompanying [JSON](2026-11-03-azure-pilot.json) retains safe timings, run IDs and one
complete metadata timeline; it contains no message bodies or restricted tool output.

## Environment and source

The source was subsequently committed as `69a423115c4b8a9ffa58b7bdb65805f653b0629e` on
codex/active-session-retention. The deployed image was built from the implementation
worktree before that commit. Its immutable digest and declared environment are in the JSON;
the [deployment guide](../../infra/azure/LIVE.md) records the release and validation commands.

The pilot used regional gpt-4o 2024-11-20 in Sweden Central, Standard capacity 10,
one 0.5-CPU/1-GiB replica per API/coordinator/tool role, private PostgreSQL 17 and Managed
Redis B0. Worker polling was 250 ms; the operator client's status polling was 500 ms.
Output was capped at 256 tokens. Actual input/output usage and cache state before each
send were not retained, so the short-answer target envelope cannot be fully verified.

## Executed observations

All five calls used `uv run python -m tests.azure.chat send --text '<non-sensitive request>'`
from the repository root, sequentially in the existing conversation. Exact synthetic
requests were, in order:

1. "For this non-sensitive runtime test, remember the fictional codeword amber-lantern
   and the test number 42. Confirm both in one sentence."
2. "What codeword and test number did I give you in my previous message?"
3. "Calculate the sum of 17 and 25, then prepare a restricted report."
4. "Introduce yourself and list the tools you currently have access to. Briefly explain
   what each tool does and your main limitations."
5. "What did I ask you in my previous message?"

These supplied fixture definitions explain the workload. Response transcripts stay outside
this register. Reproducing the sequence requires a separate conversation and the same
history/order; the [collection instructions](../AGENTS.md) preserve the user's idle experiment.

| Workload | Admission seconds | Complete-answer seconds | Tool children |
| --- | ---: | ---: | ---: |
| Seed short synthetic values | 1.810 | 2.492 | 0 |
| Recall values | 0.159 | 2.511 | 0 |
| Calculation and restricted report | 0.163 | 5.835 | 2 |
| Introduction and tool listing | 0.158 | 11.936 | 0 |
| Recall previous request | 0.171 | 2.509 | 0 |

All runs completed. The calculation returned the correct total; both child tools completed
on attempt one, with report delivery metadata kept separate. The recalls identified prior
input correctly. Several answers included incorrect or misleading claims about lacking
memory; the introduction's self-description is a known output-quality defect. Successful
execution alone does not establish answer quality or make these observations a passing cohort.

CLI timing starts before POST /runs and ends after final GET /history and response extraction.
Azure CLI token acquisition happens before the timer. Setup, first-token timing and provider
token counts are excluded/unavailable. Admission is part of the total, not an additional cost.

## Recall-turn timeline

Run `3d32107b-e3ba-4c59-ab0e-3e47b0e083d2` has the following observed intervals:

| Interval | Seconds |
| --- | ---: |
| Accepted to preparation claim | 0.225578 |
| Preparation worker claim to settlement | 0.142331 |
| Preparation settlement to inference claim | 0.186453 |
| Inference worker claim to settlement | 1.253432 |
| Inference settlement to history cache-miss event | 0.515506 |
| Cache-miss to reconstructed view | 0.017460 |
| Remaining client transport/admission residual | 0.168240 |
| Client total, recorded to milliseconds | 2.509000 |

The JSON retains the emitted UTC event timestamps. Intervals were calculated by subtracting
successive stage timestamps. The residual subtracts the accepted-to-rebuilt interval from
the rounded client total; its apparent microsecond precision is not measurement accuracy.
Cross-process clocks are assumed aligned. Inference includes preparation reads, provider
network/generation and durable settlement, which have no separate spans in this release.
The final-history gap ends before reconstruction/transport, so it cannot establish PERF-004.

Evidence was collected with the operator CLI and these read-only Azure interfaces:

```sh
az containerapp logs show -g bankeriq-live-runtime \
  -n bankeriq-li-c19afe58-coordinator --tail 150 --format text
az containerapp logs show -g bankeriq-live-runtime \
  -n bankeriq-li-c19afe58-api --tail 100 --format text
```

The equivalent saved-log query is [recent-runs.kql](../queries/recent-runs.kql); filter its
export to the run ID above. The original query selected this run directly from the same
table and fields. Log Analytics and console events agreed on the stage timings. Use the
emitted timestamp rather than the ingestion timestamp when repeating the calculation.

## Limits and validation

The runtime commit passed 491 tests with real local PostgreSQL/Redis and zero skips, plus
hosted functional checks described in the deployment guide. Recording these observations
does not rerun that suite or constitute a load test. The record was checked against its
local CLI/log evidence, with JSON, repository line limits and links validated separately.

The first observed admission took 1.810 seconds. It remains in the record; its cause was
not separately instrumented. No warmup procedure, fixed history-token envelope, concurrent
cohort, request-level provider tracing or client-visible token streaming was measured.
No percentile target is marked passed or failed from these heterogeneous observations.
The proposed monitoring expansion and the one-to-two-hour idle experiment remain separate work.
