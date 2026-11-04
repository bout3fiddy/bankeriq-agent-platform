# Hosted 5-session two-token control, 4 November 2026

The workload scheduled 100 measured arrivals; 100 finished correctly on their first subscription.
Including read-only recovery, 100 measured turns were confirmed correct.
Statuses before recovery: `{"completed": 100}`.
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

The cohort offered 5 fresh conversations every 15 seconds,
twenty turns/minute. Token acquisition, conversation setup and clock-probe setup are outside
timed turns. Each run selected eight worker attempts, with the complete in-flight allowance
reserved before execution. Usage including warmup was 101 calls,
140,592 input and 202 output tokens.
Observed peak provider overlap was 5. Telemetry matched
101/101 calls; provider statuses were
`{"200": 101}`.

| Boundary | Comparable successes | Median | Nearest-rank p95 |
| --- | ---: | ---: | ---: |
| Admission | 100 | 0.124 s | 0.192 s |
| First visible text | 100 | 1.290 s | 1.459 s |
| Complete answer | 100 | 1.405 s | 1.616 s |
| Application overhead | 100 | 0.939 s | 1.165 s |
| Terminal-write delivery lower bound | 100 | 0.073 s | 0.102 s |
| Terminal-write delivery upper bound | 100 | 0.214 s | 0.252 s |

Preparation due-to-claim upper p95/max was 290/357 ms;
inference was 288/326 ms. These bounds include claim
work. Exact Azure commit timestamps remain unavailable; terminal-write time precedes commit.
Retain both clock-offset bounds. An upper bound above 200 ms cannot establish PERF-004 compliance
or prove its violation. Overhead subtracts the single matched provider call from client duration;
it includes transport, queues, storage and application work.

This control is deliberately shorter than the required 50–100-token representative answer.
It isolates application/streaming overhead and cannot qualify the realistic short-answer cohort.

## Reproduction and limits

```sh
uv run python scratch/group12/run-wake-cohort.py --sessions 5 --name wake-control-5 --control
uv run python scratch/group12/export-wake-cohort.py wake-control-5
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

[data]: 2026-11-04-hosted-wake-control-5.json
