# Independent worker regression, 4 November 2026

All 395 accepted turns completed correctly across two independent worker processes and one API
process. Five scheduled serial arrivals remained client backlog. Twenty overloaded subscriptions
were rejected after admission and recovered their original durable runs after the burst drained.
[All observations](2026-11-04-worker-wake-independent.json) retain those original outcomes.

The workload used real local PostgreSQL 17.11 and Redis 7.4.12, with two preparation and two
inference slots per worker, four global/tenant slots and four primary connections per process.
The synthetic model emits the fixed answer in three chunks over 120 ms. Each 1/2/5/10-session
cohort has 100 scheduled attempts plus one separate warmup, at 120 offered turns/minute.
Fresh conversations keep the input comparable. There are no live provider calls.

This validates ownership batching, completion-triggered wake-up, active-source reuse and
inspection clock timing together. Optional reduced admission was added afterward and has
separate real-primary admission/exhaustion tests. The default allowance in this run was 100.
No hosted latency qualification or isolated before/after speedup is inferred from this regression.

| Sessions | Initially correct | Backlog | Recovered | Final p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 95 | 5 | 0 | 263 / 520 ms |
| 2 | 100 | 0 | 0 | 268 / 286 ms |
| 5 | 100 | 0 | 0 | 286 / 454 ms |
| 10 | 80 | 0 | 20 | 391 / 624 ms |

Medians and nearest-rank p95 describe the initially correct samples only. Serial and overload
cohorts are incomplete and therefore cannot qualify their latency distribution. Recovered
answers retain separate timing. All processes drained; cleanup removed only the owned schema
and child processes, preserving shared Redis and the hosted deployment.

Executed command:

```sh
BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
  BANKERIQ_TEST_REDIS_URL=redis://127.0.0.1:56399/0 \
  uv run python -m tests.scaling.conversations \
  --output scratch/group12/worker-wake-independent --workers 2 --slots 2 \
  --prepare-slots 2 --capacity 4 --count 100 --interval 0.5
```

Use a fresh output directory for reproduction. Process logs and raw records remain local.
Final verification after the optional budget addition passed 717 tests with zero skips in
141.47 seconds. The focused budget tests cover default/lower admission, duplicate conflicts,
invalid increases and stopping before an unfunded inference claim.
