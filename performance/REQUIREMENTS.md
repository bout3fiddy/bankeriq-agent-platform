# Pilot performance requirements

Status: proposed engineering targets. Recorded on 3 November 2026.
These targets are not achieved service commitments or Azure guarantees. Performance
optimisation is deferred beyond the current active-session-retention change.

## Workload envelope

Evaluate one and two concurrent active conversations as separate cohorts, using the
same deployment, fixed representative prompt/history size and non-sensitive fixtures.
The short-answer cohort has no tools or approval waits and at most 100 output tokens.
Record model/version, actual token counts, replicas, queue limits and cache state.
Record first-use requests separately from steady-state requests; preserve both results.
Long answers, tool workflows, failures and recovery require separately named workloads.

## Targets and measurement boundaries

| ID | Measurement | Proposed target |
| --- | --- | --- |
| PERF-001 | Durable message admission | p95 <= 300 ms |
| PERF-002 | Application overhead outside model/tool execution | p95 <= 500 ms |
| PERF-003 | Complete short answer without tools | p50 <= 2 s; p95 <= 5 s |
| PERF-004 | Durable completion to final answer visible to client | p95 <= 200 ms |
| PERF-005 | First visible model text, once streaming exists | p95 <= 1.5 s |

- PERF-001 starts immediately before the client's POST /runs and ends after its
  successful response. Include API authentication, transport and durable acceptance.
  The existing CLI acquires its Entra token before this timer; record that exclusion.
- PERF-002 includes application, queue, storage/cache and client-delivery overhead on
  the turn's critical path. Exclude external model/tool execution using explicit spans.
  Parallel operations overlap: subtract their union on the critical path, never the sum
  of all child durations. The current whole inference-worker span also contains database
  work and cannot supply an exact model-only subtraction.
- PERF-003 starts before POST /runs and ends when the client has the final assistant
  answer. The current CLI includes polling and GET /history in this timer. A failed or
  truncated answer is not a successful latency sample. Preserve its failure record.
- PERF-004 starts at the durable terminal commit and ends when the final answer reaches
  the client. A worker.settled timestamp is a post-commit approximation. The historical
  interval ending at cache.miss excludes reconstruction and response transport; it is
  not this complete measurement.
- PERF-005 measures the client's first visible model text, excluding an acknowledgement,
  heartbeat or status event. The current provider adapter uses stream=False and the CLI
  displays a completed answer, so this requirement is not currently measurable.

## Qualification and reporting

- Keep at least 100 comparable measured attempts per cohort for an initial assessment.
  This is a collection minimum, not a statistical confidence guarantee. Retain all
  attempts, state the successful-completion denominator, and report failures/timeouts.
- Publish p50 and p95 with sample counts and the calculation method. Use nearest-rank
  p95: sort the successful durations and select position ceil(0.95 * n), counting from one.
  Use the ordinary median for p50. Do not combine different workloads to reach the minimum.
- Timing compliance alone does not establish acceptance. Review output correctness,
  expected tools, tenant isolation, durable completion and absence of restricted output.
  A faster failed request does not count as a passing result.
- Keep first-use/cache-cold observations and steady-state results in separate cohorts;
  never remove slow samples without a recorded reason. Record overload and throttling.
- Mark each target as measured or unmeasured and state whether the workload qualifies.
  Existing five-turn observations establish no percentile compliance. Their actual
  token counts are unavailable, so short-answer eligibility is not fully verified.
- Keep raw safe measurements alongside each report. Record a code commit and deployed
  image digest; local HEAD alone does not prove which image Azure is running.

## Reliability and operational boundaries

Do not trade permission checks, lease fencing, budgets, durable admission, unknown-effect
reconciliation or restricted-output isolation for speed. Monitoring, status reads and
open SSE connections must remain passive for conversation inactivity accounting.

Production load, availability objectives, storage capacity, autoscaling, per-tool budgets
and alert thresholds need separate evidence. The live pilot's one-to-two-hour idle experiment
remains a separate endurance check. Broader tracing, metrics and alerting are proposed work.

Output length affects generation time. Pair latency with input/output token counts and
separate first-token latency from complete-answer latency; see [Azure's latency guide][latency].
Percentiles should describe the relevant user journey; see [Google's SLO guidance][slos].
The numerical targets above are this project's proposed choices.

[latency]: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/latency
[slos]: https://sre.google/workbook/implementing-slos/
