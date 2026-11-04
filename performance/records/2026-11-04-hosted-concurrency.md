# Hosted concurrent conversations, 4 November 2026

The two- and five-session cohorts completed all 100 measured turns correctly. Five provider
calls overlapped, with no provider throttling or lost accepted input. Group 12 remains
unqualified: first-text latency and application overhead miss their targets; the single-session
and overload cohorts are incomplete. [Safe measurements](2026-11-04-hosted-concurrency.json)
retain every scheduled outcome, separate warmups, recovery observations and numeric spans.

## Deployment and workload

Collection ran from 13:36 to 13:57 UTC against revision 8. Source HEAD was
`54440b894deefad6dbfefe70252bfab37013fa84` plus uncommitted group-12 changes. Image:
`sha256:6140fa844b4d4cd4f220d8358f5f21ec280b37b5a2a20abe8a05ec03d280a45c`.
Later local memory/prompt changes are outside this measurement.

Sweden Central hosted one API, coordinator and tool replica, each 0.5 CPU/1 GiB with pool
size four. Foundry was regional Standard `gpt-4o-2024-11-20`, configured at 50,000 TPM.
Coordinator capacity was two preparation slots and five inference slots; tenant inference
capacity was five. Every attempt used a fresh chat under the same authorised Entra operator.

The fixed paragraph used 1,389 actual input tokens and 67–68 output tokens per successful
call. Each cohort scheduled 100 attempts at 20 offered turns/minute, plus one separate warmup.
Batches of 1/2/5/10 arrived every 3/6/15/30 seconds. Token acquisition and conversation creation
preceded timing. No other model workload ran concurrently. Controlled alert fixtures produced
synthetic error telemetry during tier 2; those fixture IDs are excluded from genuine failures.

## Results

Ordinary median and nearest-rank p95 below use initially successful measured turns. Counts
remain separate; recovery durations are not substituted into the original sample. The 1/10
rows are diagnostic observations with fewer than 100 successful samples.

| Sessions | Initially correct / scheduled | Admission p95 | First text p95 | Final p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 91 / 100 | 123 ms | 2.095 s | 1.679 / 3.093 s |
| 2 | 100 / 100 | 129 ms | 1.755 s | 1.635 / 2.372 s |
| 5 | 100 / 100 | 253 ms | 2.408 s | 2.114 / 2.811 s |
| 10 | 39 / 100 | 457 ms | 4.072 s | 2.179 / 6.019 s |

For tiers 1/2/5/10 respectively, estimated overhead p95 was 1.019/0.835/1.391/2.623 seconds.
The workload has one sequential model call, so overhead subtracts its observed provider span
from client completion time. It includes application, storage, transport and delivery work.
PERF-001 passes at two/five sessions. PERF-002 and PERF-005 miss in both qualified cohorts.
PERF-003 passes at two sessions; five-session median exceeds the two-second target.

Terminal write-to-delivery conservative upper bounds were 167/222/272/352 ms at p95.
The write precedes commit and the client includes clock uncertainty. Azure commit timestamps
were unavailable: values above 200 ms leave PERF-004 unproven and do not establish its exact
commit-to-delivery latency. No post-commit approximation is presented as an exact commit time.

Preparation queue upper-bound p95/max was 389/689, 204/370, 768/999 and 1,284/1,418 ms.
Inference was 205/471, 194/429, 254/526 and 1,208/1,523 ms. These conservative observations
include the claim transaction through commit acknowledgement and can include pre-eligibility
capacity/resource waits. The five-session preparation observation exceeds the 500 ms target.
Increasing local preparation slots from two to five did not materially improve completion time;
that separate synthetic experiment does not justify a hosted capacity change.

## Incomplete arrivals and recovery

Nine serial arrivals remained client backlog because the preceding turn was unfinished. They
were never POSTed. The historical revision-7 cohort had five such outcomes; neither qualifies
100 successful serial samples. Slightly lower revision-8 medians establish no reliable speedup.

At ten sessions, nine subscriptions received bounded SSE 429 rejection while their runs stayed
accepted; all recovered their correct durable answer. One additional client timed out after
an inference stream lost its response. There was one client-backlog arrival. The campaign then
stopped 50 scheduled arrivals when unknown usage was detected. All 49 measured accepted runs,
plus the separate warmup, eventually completed correctly. Tenant inference peaked at five.

Run `1d49f1e5-2c24-406d-ac02-48af773d704d` emitted first text at 1.56 seconds, then its first
provider stream timed out after 30.590 seconds with transport status zero and no usage frame.
The durable retry used the same prepared job and completed in 760 ms. Settlement occurred
at 13:54:17.904 UTC; passive recovery later confirmed exactly one final answer and no unfinished
release work. The original timeout and stopped arrivals remain in the record.

Total calls including warmups were 92/101/101/51. Tier 10 had 50 known calls using 69,450 input
and 3,350 output tokens, plus one unknown attempt. The finite campaign additionally reserves
8,192 input and 512 output tokens for that already-counted attempt. These are conservative
reservations, not reported actual billing. No unknown tool effect was reset or replayed.

## Reproduction and limits

Use the authorised operator configuration and a fresh output/stop path for each tier:

```sh
uv run python -m performance.workloads.concurrent_answers \
  --config scratch/live-client.json --output scratch/new-hosted-tier5.json \
  --stop scratch/STOP-new-hosted-tier5 --sessions 5 --count 100 \
  --tenant '<authorised-tenant-id>' --user '<authorised-operator-id>' \
  --image-digest '<verified-immutable-image-digest>' \
  --budget-input-tokens 182000 --budget-output-tokens 8000 --max-output-tokens 75
```

Repeat separately with sessions 1/2/10 under an explicitly finite campaign budget. Stop on
unknown usage, inspect durable state and retain its maximum reservation before further calls.
Executed commands, reconciliation and numeric queries remain in ignored `scratch/group12/`:
`measured-tier-*.json`, `measured-tier-*-spans.kql`, `measured-tier-10-reconciliation.json`,
`export-concurrency.py` and `current-stage.json`. The exporter retains only metadata.

Revision-8 process observations are sampled every 30 seconds and cannot capture every short
pool wait or RSS peak. The earlier two-hour soak lacked actual RSS/tasks/pool-wait measurements.
Mixed ordinary/compaction traffic, the control workload, complete overload collection and
resource qualification remain separate outstanding gates. No production autoscaling or HA
claim follows from these fixed-replica cohorts.
