# Hosted streaming baseline, 4 November 2026

The first realistic-output hosted cohort does **not** qualify group 12. All 95 accepted
measured turns completed correctly, with no provider errors or unknown usage. Five of 100
scheduled arrivals remained client backlog because the preceding single-session turn had
not finished. Preserve those outcomes; they were never submitted or silently retried.

[Safe measurements](2026-11-04-hosted-streaming-baseline.json) retain every scheduled outcome,
one separate warmup and correlated numeric spans. Collection ran at 11:46–11:52 UTC.

## Deployment and workload

- Source HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus uncommitted group-12 changes.
- Immutable deployed image digest:
  `sha256:93f4cb61d246cc2d2b83751f5a390b880b09fb47bebf3dac0d2a5dfa5d073de7`.
- Sweden Central; Foundry `gpt-4o-2024-11-20`, regional Standard, 50,000 TPM capacity.
- One API, coordinator and tool replica, each 0.5 CPU/1 GiB; primary pools four per process.
- One active conversation; a fresh conversation per turn, 20 offered turns/minute.
  Two other soak conversations maintained passive SSE connections without model traffic.
- Fixed reviewed paragraph: 1,389 actual input tokens and 67–68 output tokens per call.
  Authentication/token acquisition and conversation creation precede turn timing.
- Actual usage including warmup: 96 calls, 133,344 input tokens, 6,433 output tokens.

## Observations

Nearest-rank p95 and ordinary median below use the 95 accepted measured turns. This is
below the 100-successful-sample minimum for comparable timing qualification.

| Boundary | p50 | p95 | Target assessment |
| --- | ---: | ---: | --- |
| Client durable admission | 114 ms | 146 ms | Within 300 ms diagnostic target |
| First visible model text | 1.35 s | 1.96 s | Above 1.5 s target |
| Correct final answer | 1.80 s | 2.86 s | Within 2 s/5 s diagnostic targets |
| Estimated non-model overhead | 743 ms | 1,042 ms | Above 500 ms target |
| Terminal write-to-delivery upper bound | — | 172 ms | Below 200 ms conservatively |

The terminal write occurs before commit. The client retains its server-clock uncertainty
interval; the upper end therefore conservatively bounds commit-to-delivery. Azure commit
timestamps were unavailable. This does not claim an exact durable commit timestamp.
The one-model-call workload permits subtracting its observed provider duration from total
client time. Provider and publication work overlap; no child durations are added together.

Offered throughput was 20/minute; achieved measured throughput was 19.06/minute. Candidate age
p95 was 394 ms for preparation and 184 ms for inference; maximums were 422 and 731 ms.
These samples end at the candidate query, before ownership writes and commit acknowledgement.
They cannot establish the complete eligible-job wait gate. A subsequent source change adds
a conservative through-commit upper bound; this historical image does not emit it.

Preparation-worker duration p95 was 235 ms; memory assembly p95 was 160 ms. Provider duration
p95 was 2.10 seconds. A representative trace spent about 0.6 seconds between admission and
provider start. Final delivery is already fast enough in this sample; preparation and
context/ownership reads account for substantial pre-generation work. These observations do
not establish that any individual permission or fencing check can be removed safely.

## Reproduction

Use the currently authorised operator configuration and verify its endpoint/image first.
The program uses a fresh chat per attempt and leaves the supplied configuration unchanged.
The declared budget includes warmup. Choose new output/stop paths for each collection.

```sh
uv run python -m performance.workloads.concurrent_answers \
  --config scratch/live-client.json --output scratch/new-hosted-tier1.json \
  --stop scratch/STOP-new-hosted-tier1 --sessions 1 --count 100 \
  --tenant '<authorised-tenant-id>' --user '<authorised-operator-id>' \
  --image-digest '<verified-immutable-image-digest>' \
  --budget-input-tokens 182000 --budget-output-tokens 8000 --max-output-tokens 75
```

The executed command and source hashes are retained in the local group-12 evidence. Fetch
numeric lifecycle spans after ingestion using the existing performance queries. Hosted
process RSS/tasks/pool waits were unavailable in this image. Separate Azure Monitor working
set/CPU/replica and database connection observations belong to the ongoing soak record.
Tiers 2/5/10, the control, mixed compaction and memory-quality gates remain outstanding.
