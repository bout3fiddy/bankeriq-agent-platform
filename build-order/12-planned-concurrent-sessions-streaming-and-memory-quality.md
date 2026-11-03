# Concurrent sessions, streaming and memory quality

Status: planned. Revised on 4 November 2026. Depends on groups 07–09 and
[group 11](11-completed-semantic-compaction-and-retrieval.md). This is the next implementation
group. Group 10's business tools and group 13's workforce onboarding are not prerequisites.

## Purpose and baseline

Demonstrate that several independent conversations can make progress concurrently, deliver
useful streamed answers and retain accurate memory. Measure the operating envelope and improve
the bottlenecks revealed by that workload. Preserve the synchronous in-memory profile.

The [3 November measurements](../performance/records/2026-11-03-responsive-compaction.md)
established 1.24–1.26-second median and 1.62–1.65-second p95 completion for two-token replies.
Both cohorts offered at most 20 messages/minute; they establish no saturation capacity.
Estimated non-model overhead was about 1.2 seconds at p95 against a 500-ms target. Streaming
and exact commit-to-delivery timing were unmeasured. Memory trials exposed omitted identities,
delayed correction handling and failure to retrieve an original detail automatically. An
incompatible-profile rollout also failed one request while old and new consumers overlapped.

Storage retention, database growth, archival, persistent storage quotas and conditional sharding
belong to [group 14](14-planned-storage-lifecycle-and-capacity-controls.md). Workforce identity
and user onboarding belong to [group 13](13-planned-workforce-identity-and-access-onboarding.md).
Broker transport and business prompt overlays remain in the [deferred options](deferred-options.md).

## Planned requirements

1. **Define concurrent conversation workloads** — Exercise 1, 2, 5 and 10 active sessions as
   separate tiers, including users with multiple sessions and multiple users/tenants. Distinguish
   open SSE connections, active conversations and simultaneously executing model/tool jobs.
   Cover short answers, longer streamed answers, bounded parallel tools, history retrieval and
   repeated compaction. Include unequal conversation lengths, slow tools, cancellation and
   reconnects. Use synthetic identities locally; an authorised hosted operator can own several
   isolated test sessions before group 13 provides real workforce onboarding.
2. **Measure capacity and latency honestly** — Extend the existing performance harnesses and
   [requirements](../performance/REQUIREMENTS.md). Record offered and achieved throughput,
   session count, simultaneous in-flight jobs, request cadence, tokens, provider RPM/TPM,
   queue age, pool/lock waits, CPU, memory and database connections. Separate cold/warm views,
   prompt/history sizes, idle recovery, tool work and profile-upgrade rebuilds. Use both paced
   arrival workloads and bounded bursts; expose client-side backlog and coordinated omission.
   Preserve failures, 429s and rejected admissions. Declare the target operating envelope and
   finite load/cost limits before collecting acceptance data; higher tiers may be overload tests.
3. **Attribute and reduce overhead** — Add correlated, payload-safe spans across API admission,
   queue eligibility/claim, context assembly, model transport, compaction, tool execution,
   settlement and final client delivery. Measure first visible text and durable commit to client
   completion. Exclude overlapping external operations by their critical-path union. Profile
   the measured bottleneck before changing polling, connection pools or scheduling. Preserve
   permissions, ownership fencing, retry/attempt budgets and unknown-effect reconciliation.
   Genuine provider throttles retain bounded durable retries; delayed work releases workers.
4. **Stream permitted model text** — Extend the provider adapter and queued API through the
   existing bounded Redis/SSE progress path. Keep clients/network I/O outside core runtime logic.
   Authorise every subscription and apply output policy before publishing eligible text. Mark
   partial text provisional; attach run/attempt identity and sequence so clients discard stale
   attempts. Accumulate and validate complete tool calls before admission; partial tool arguments
   never dispatch work. A durable final answer replaces provisional text exactly once. Handle
   provider disconnects, cancellation, partial usage and retries without manufacturing a completed
   answer or replaying uncertain effects. Required full-message validation may delay publication;
   document that boundary rather than weakening enforcement to meet a timing target.
5. **Bound streaming resources and recover reconnects** — Test byte/event limits, slow clients,
   connection caps, Redis loss, reconnect on another API replica and worker restart mid-stream.
   Expose an explicit progress gap/reset and reconstruct the durable answer from PostgreSQL.
   One slow reader must not stall inference, settlement or other sessions. Idle conversations
   and open SSE connections must not occupy worker execution slots or renew session activity.
   Keep report delivery metadata separate and restricted contents absent from chunks and logs.
6. **Improve memory fidelity and retrieval choice** — Build reviewed dialogues with expected
   facts, corrections, unresolved/resolved questions, identities, ownership, amounts/currencies,
   historical values and actual tool outcomes. Evaluate answers as well as source selections.
   Make missing-source questions trigger bounded authorised retrieval when appropriate, then
   answer from evidence or state the remaining uncertainty. Prevent stale facts, duplicated
   questions and prototype/project-name confusion from recurring across compaction cycles.
   Extend beyond English-only retention cues with declared language cases. Keep source links,
   original active input, recent verbatim context, bounded protected facts and scoped retrieval.
   User assertions cannot become approvals or completed actions. Cache loss must reuse compatible
   durable memory; separate profile rebuild cost from normal compaction and Redis recovery.
7. **Validate fair scheduling and replica behaviour** — Compare one and multiple independent
   coordinator/tool processes with identical total capacity, then measure increased capacity.
   Ensure one busy session or tenant cannot indefinitely starve another within the declared
   envelope. Preserve per-conversation ordering, child ownership and one durable join. Reserve
   admission/settlement capacity under backlog. Tune limits from evidence; add priorities or
   automatic scaling only when justified and supported by the host/authentication combination.
   Document fixed-replica limits and the current single-primary database availability boundary.
   Reuse group 09's recovery evidence when designing any separately authorised HA change.
8. **Make releases safe for active sessions** — Implement and test a drain/routing procedure
   for incompatible prompt/memory/runtime profiles. Define admission behaviour during cutover,
   allow pinned work to finish or remain recoverable on a compatible worker, and verify old
   consumers have stopped before reopening incompatible work. Test rollback with pending model,
   tool and compaction jobs. Report any maintenance interval; new HTTP readiness alone is not
   evidence of queue-consumer compatibility or zero downtime.
9. **Make failures observable** — Reuse lifecycle/model events and the existing Azure logging
   path. Add a small operator dashboard/query set and actionable alerts for queue age, error/
   throttle rate, stuck work, streaming failures and missing telemetry. Correlate run/step/job
   IDs without prompts, answers, credentials or restricted payloads. Select sampling, retention
   and ingestion budgets; record dropped telemetry. Evaluate OpenTelemetry/Application Insights
   at the application boundary when needed for tracing. Infrastructure changes still require
   reviewed Terraform plans and deployment authorisation. Storage-growth alerts belong to 14.

## Implementation anchors

- Core request/reply and output boundaries: `src/agent_runtime/coordinator/`,
  `src/agent_runtime/providers/foundry/` and existing gateway/output enforcement.
- Queued execution and progress: `src/bankeriq_agent_platform/workers/`,
  `src/bankeriq_agent_platform/sessions/` and `src/bankeriq_agent_platform/runtime.py`.
- Semantic memory: `src/agent_runtime/conversations/` and
  `src/bankeriq_agent_platform/memory/`; inspect migrations 013–015 before extending state.
- Measurements: `performance/workloads/`, `performance/queries/`, `tests/scaling/`,
  `tests/integration/`, `tests/azure/` and `src/bankeriq_agent_platform/telemetry.py`.
- Hosted settings and release controls: `infra/azure/` and its
  [inventory](../infra/azure/README.md).

## Acceptance gates

- Publish reproducible 1/2/5/10-session results with at least 100 measured attempts per comparable
  latency cohort, separate warmups and all failures. Demonstrate actual overlapping work and
  independent progress. Record the sustainable envelope and explicit overload behaviour; do not
  pool different workloads or equate connected users with throughput capacity.
- Within the declared short-answer envelope, meet PERF-001 through PERF-005 using their exact
  boundaries. Include realistic bounded output lengths alongside the historical two-token
  control. Record before/after attribution of PERF-002 and observed concurrent degradation.
  An unmet target leaves this group in-progress; a target change needs an explicit decision.
- Streaming tests prove ordered eligible chunks, bounded buffers, safe cancellation and retry,
  stale-attempt rejection, one durable final answer and reconnect across replicas/Redis loss.
  A deliberately slow client cannot increase buffers beyond their bounds or block other users.
- A reviewed suite contains at least 20 distinct memory scenarios, repeated twice against the
  live model. Selected long dialogues undergo at least five compaction cycles. Every designated
  critical fact/correction and tool-state assertion must be correct; at least 95% of other
  expected facts must be recovered. Include automatic retrieval of omitted source details,
  multilingual cases, tenant isolation, deletion/access changes and bounded-context failures.
  Retain failed transcripts and judge semantic relevance independently of quotation validity.
- At least 20 representative live summary generations finish within ten seconds each under
  the declared warm workload and pass fidelity checks. Measure summary publication separately.
  Collect 100 comparable samples before claiming a compaction percentile; record cold profile
  rebuilds separately. Concurrent compaction must not starve ordinary accepted turns.
- Real local PostgreSQL/Redis and independent worker processes pass cancellation, duplicate
  delivery, slow-consumer, fairness, retry, restart and profile-cutover scenarios. No duplicated
  effects/joins, lost accepted input, cross-session outputs or restricted-data exposure occurs.
  Both compatible rolling updates and incompatible drain/rollback have executed evidence.
- Exercise the alerts with controlled failures and verify correlation, delivery and recovery.
  Publish dashboards/runbooks, measured replica/resource limits and any HA/autoscaler gaps.
  Hosted load, rollout and inference checks require explicit deployment/load authorisation.

## Evidence and handoff

Planning only; no group-12 implementation or new measurements are claimed. During execution,
run `uv run python scripts/verify.py` with real local PostgreSQL/Redis configured, plus
`git diff --check` and the workload-specific commands. Skipped service tests cannot satisfy gates.
Record commands, fixtures, versions, source commit, image digest, load bounds and outcomes here.
Keep raw transcripts in ignored `build-order/evidence/` and safe dated measurements under
`performance/records/`; update the performance index without rewriting historical results.
