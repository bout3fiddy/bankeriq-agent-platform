# Local preparation capacity, 4 November 2026

Increasing preparation slots from two to five did not materially improve overall completion.
The hosted setting remains two. This [paired record](2026-11-04-preparation-capacity.json)
retains 800 scheduled local attempts and separate warmups, including rejected subscriptions,
backlog and later recovery. Every accepted run eventually completed with the expected text.

Both configurations used one API and one independent worker process, five inference slots,
global/tenant queue capacity eight, and four primary connections per process. PostgreSQL 17.11
and Redis 7.4.12 were real loopback services; Python was 3.14.8 on macOS. The synthetic model
emitted fixed text in three chunks with 120 ms total simulated delay. No provider was called.
The live-memory evaluator shared the local primary during this experiment, using a separate
schema. Its load was not controlled; this is one exploratory comparison, not an isolated
causal estimate or a hosted speedup claim.

Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes, before the
later v9 memory/prompt edits. Each tier offered 120 turns/minute in batches of 1/2/5/10 at
0.5/1/2.5/5 seconds. Correct initial completion p50/p95 uses median/nearest rank. Incomplete
serial and overload distributions are diagnostic; separate recovered turns are not pooled.

| Sessions | Prepare slots | Initially correct | Backlog | Recovered | Final p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 76 | 24 | 0 | 277 / 768 ms |
| 1 | 5 | 70 | 30 | 0 | 281 / 809 ms |
| 2 | 2 | 100 | 0 | 0 | 273 / 309 ms |
| 2 | 5 | 100 | 0 | 0 | 279 / 554 ms |
| 5 | 2 | 100 | 0 | 0 | 339 / 365 ms |
| 5 | 5 | 100 | 0 | 0 | 347 / 361 ms |
| 10 | 2 | 80 | 0 | 20 | 375 / 751 ms |
| 10 | 5 | 80 | 0 | 20 | 376 / 720 ms |

At five sessions, first-text p95 decreased from 301 to 258 ms, while final completion barely
changed. At two sessions p95 worsened. These mixed results do not justify increasing hosted
preparation concurrency. The ten-session recoveries followed the existing per-user SSE cap;
accepted input remained durable. The serial client explicitly retained missed arrival slots.

Executed commands, from the repository root with disposable service DSNs:

```sh
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56399/0'
uv run python -m tests.scaling.conversations --output scratch/new-prepare-two \
  --workers 1 --slots 5 --prepare-slots 2 --capacity 8 --count 100 --interval 0.5
uv run python -m tests.scaling.conversations --output scratch/new-prepare-five \
  --workers 1 --slots 5 --prepare-slots 5 --capacity 8 --count 100 --interval 0.5
```

Choose fresh output directories. The executed artifacts are `scratch/group12/prepare-two/`,
`prepare-five/` and `run-prepare-capacity.sh`. Each run removed only its owned schema/processes;
Redis was not flushed. Global worker-count/total-capacity comparisons remain separate records.
