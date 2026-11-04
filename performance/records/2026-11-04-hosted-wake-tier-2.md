# Hosted 2-session fixed-paragraph streaming, 4 November 2026

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

The cohort offered 2 fresh conversations every 6 seconds,
twenty turns/minute. Token acquisition, conversation setup and clock-probe setup are outside
timed turns. Each run selected eight worker attempts, with the complete in-flight allowance
reserved before execution. Usage including warmup was 101 calls,
147,157 input and 6,767 output tokens.
Observed peak provider overlap was 2. Telemetry matched
101/101 calls; provider statuses were
`{"200": 101}`.

| Boundary | Comparable successes | Median | Nearest-rank p95 |
| --- | ---: | ---: | ---: |
| Admission | 100 | 0.079 s | 0.109 s |
| First visible text | 100 | 1.050 s | 1.302 s |
| Complete answer | 100 | 1.408 s | 1.919 s |
| Application overhead | 100 | 0.583 s | 0.689 s |
| Terminal-write delivery lower bound | 100 | 0.069 s | 0.104 s |
| Terminal-write delivery upper bound | 100 | 0.130 s | 0.230 s |

Preparation due-to-claim upper p95/max was 156/239 ms;
inference was 181/199 ms. These bounds include claim
work. Exact Azure commit timestamps remain unavailable; terminal-write time precedes commit.
Retain both clock-offset bounds. An upper bound above 200 ms cannot establish PERF-004 compliance
or prove its violation. Overhead subtracts the single matched provider call from client duration;
it includes transport, queues, storage and application work.

The representative answer has 67 output tokens and 1,457 input tokens. Admission and completion
meet their targets when all 100 turns are correct. Assess first text against 1.5 seconds and
application overhead against 500 ms; an unmet target keeps group 12 in-progress.

## Reproduction and limits

```sh
uv run python scratch/group12/run-wake-cohort.py --sessions 2 --name wake-tier-2
uv run python scratch/group12/export-wake-cohort.py wake-tier-2
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

[data]: 2026-11-04-hosted-wake-tier-2.json

First-text p95 1.302 seconds passes the 1.5-second target; overhead p95 689 ms exceeds
500 ms. Final-delivery upper p95 230 ms leaves that gate unqualified. Queue upper bounds
meet both p95 and maximum targets. All 100 ordinary arrivals completed correctly.
