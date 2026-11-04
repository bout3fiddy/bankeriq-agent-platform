# Hosted mixed conversation workload, 4 November 2026

All 406 accepted measured turns completed correctly. The 408 scheduled arrivals include two
client-backlog outcomes after one slow provider call. The 101 warm one-compaction turns had
completion p95 3.146 seconds and summary-generation p95 1.073 seconds. Group 12 remains
in-progress because other latency gates still miss. [Safe metadata](2026-11-04-hosted-mixed.json)
retains every arrival, warmup, publication count and matched provider observation.

## Workload and deployment

Revision 10 used
`sha256:94f2a1ead6269dea8131e5ba4715fde53c356b08638b805eec65d828e08db0e9`,
source HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes, memory v10 and
system prompt 1.2.0. One coordinator had two preparation, five inference and two summary slots,
four database connections, 0.5 CPU and 1 GiB. PostgreSQL 17, Redis Balanced B0 and regional
Foundry gpt-4o 2024-11-20 at 50,000 TPM used the existing Sweden Central environment.

Two lanes offered paired ordinary/memory turns every twelve seconds: ten turns/minute.
The original 200 pairs produced 99 one-compaction turns. Four explicit continuation pairs
used the same warm memory conversation, image, cadence and fixture, with separate raw evidence
and accounting. The original collection was preserved. Five memory warmups remain separate.
All 516 provider calls matched numeric telemetry: HTTP 200, 1,640,006 input and 15,430 output
tokens, with no unresolved usage. This experiment ran for approximately 41 minutes overall.

## Results

Median and nearest-rank p95 use successful measured turns in each named category.

| Category | Count | Completion p50 | Completion p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Ordinary | 202 | 1.709 s | 2.871 s | 24.584 s |
| Memory, no compaction | 102 | 1.763 s | 2.563 s | 8.359 s |
| Memory, exactly one compaction | 101 | 2.522 s | 3.146 s | 4.085 s |
| Ordinary with actual summary overlap | 103 | 1.684 s | 2.871 s | 24.584 s |

One further memory turn made two summaries and finished in 3.001 seconds; it is excluded from
the one-compaction percentile. Provider intervals establish actual overlap in 103 ordinary
turns, using one coordinator's event timestamps and monotonic adapter durations. That subset
meets the five-second complete-answer p95 target during compaction. Ordinary first-text p95
was 2.050 seconds overall, exceeding PERF-005. The incomplete arrival denominator prevents
claiming an all-arrivals pass. This workload does not measure exact commit-to-delivery latency.

The 101 eligible summary generations took median 644 ms, p95 1,073 ms and maximum 2,288 ms.
No summary call, including warmups and the two-summary turn, exceeded ten seconds. Generation
end to validated publication took median 38.598 ms, p95 73.396 ms and maximum 91.076 ms.
That latter interval includes validation and database publication; it is not pure SQL time.
The separate [memory suite](2026-11-04-memory-coverage.md) establishes semantic fidelity;
this repetitive synthetic fixture tests scheduling, transport correctness and compaction.

The slow ordinary turn spent 23.745 seconds inside its single successful provider call,
versus 24.584 seconds end to end. It returned 67 tokens without a throttle or retry. Two next
scheduled ordinary arrivals remained client backlog, while the memory lane continued.
Those outcomes are retained; no model response or arrival was silently resubmitted.

## Reproduction and limitations

Executed wrappers, finite allocations and raw queries remain in ignored scratch:

```sh
uv run python scratch/group12/run-coverage-mixed.py
uv run python scratch/group12/run-mixed-continuation.py
uv run python scratch/group12/export-mixed.py
```

Both wrappers use the tracked mixed workload with fresh evidence paths. The continuation
reuses the explicitly selected warm conversation; it must not start a second campaign at once.
Before repeating, inspect the image, reserve the entire bounded workload from the authorised
remaining campaign allowance, and use fresh paths. Unknown usage retains its reservation.
See [the workload guide](../../tests/mixed/README.md) for options and accounting.

Raw provider intervals and publication events were queried after ingestion. The exported
ordinary-overlap subset was derived from those matched intervals. No prompts, answers,
credentials, tool payloads or unrestricted CLI responses were copied into the tracked record.
This was a single-operator test under fixed capacity, with no claim of production saturation,
multi-user identity onboarding, HA failover or long-term resource stability.
