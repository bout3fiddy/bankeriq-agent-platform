# Hosted 10-session fixed-paragraph streaming, 4 November 2026

The workload scheduled 100 measured arrivals; 80 finished correctly on their first subscription.
Including read-only recovery, 100 measured turns were confirmed correct.
Statuses before recovery: `{"accepted": 20, "completed": 80}`.
[Safe observations][data] retain every arrival, one separate warmup, clock bounds,
usage and matched provider events. Group 12 remains in-progress.

## Configuration and measurements

Revision 12 used
`sha256:543edf85d8ed885841077771da6ff4efb8ed3f9c320281dbfdae2799d76c8c19`,
source HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes.
The existing Sweden Central deployment
has one replica per role, 0.5 CPU/1 GiB each, four primary connections per process, five
preparation/inference slots and two summary slots. The provider is regional gpt-4o 2024-11-20,
50,000 TPM. Memory v10/system prompt 1.2.0 are unchanged.

The cohort offered 10 fresh conversations every 30 seconds,
twenty turns/minute. Token acquisition, conversation setup and clock-probe setup are outside
timed turns. Each run selected eight worker attempts, with the complete in-flight allowance
reserved before execution. Usage including warmup was 101 calls,
147,157 input and 6,767 output tokens.
Observed peak provider overlap was 5. Telemetry matched
101/101 calls; provider statuses were
`{"200": 101}`.

| Boundary | Comparable successes | Median | Nearest-rank p95 |
| --- | ---: | ---: | ---: |
| Admission | 80 | 0.298 s | 0.345 s |
| First visible text | 80 | 2.037 s | 3.151 s |
| Complete answer | 80 | 2.537 s | 3.579 s |
| Application overhead | 80 | 1.433 s | 2.579 s |
| Terminal-write delivery lower bound | 80 | 0.076 s | 0.107 s |
| Terminal-write delivery upper bound | 80 | 0.236 s | 0.294 s |

Preparation due-to-claim upper p95/max was 692/739 ms;
inference was 1418/1715 ms. These bounds include claim
work. Exact Azure commit timestamps remain unavailable; terminal-write time precedes commit.
Retain both clock-offset bounds. An upper bound above 200 ms cannot establish PERF-004 compliance
or prove its violation. Overhead subtracts the single matched provider call from client duration;
it includes transport, queues, storage and application work.

Ten sessions are the declared overload tier. Latencies describe only correct first-pass turns;
recovered subscription rejections retain their original failure and separate outcome. They are
excluded from successful latency percentiles. Fewer than 100 comparable successes do not qualify
a percentile gate. Accepted runs are never resubmitted to obtain their final answers.

## Reproduction and limits

```sh
uv run python scratch/group12/run-wake-cohort.py --sessions 10 --name wake-tier-10
uv run python scratch/group12/export-wake-cohort.py wake-tier-10
```

The wrapper invokes tracked `performance/workloads/concurrent_answers.py`, reserves from the
approved campaign ledger and charges complete observed usage. Unknown usage retains its
reservation. Select fresh paths and inspect the deployment before any authorised repetition.
The reference client preserves stream rejection and retries read-only recovery while retaining
its active slot. No credentials, prompts, answers or tool payloads are exported.

The deployed runtime passed 717 real-service tests. The later harness accounting fix passed
719 tests with zero skips in 141.48 seconds. Existing two-hour soak/resource observations belong
to earlier revisions. No HA, autoscaling, multi-user workforce or new-image endurance guarantee
is established by this finite single-operator cohort.

[data]: 2026-11-04-hosted-wake-tier-10.json
