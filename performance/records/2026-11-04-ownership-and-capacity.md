# Ownership checks and increased local capacity, 4 November 2026

[Measurements](2026-11-04-ownership-and-capacity.json) retain four ownership probes and
800 scheduled local HTTP attempts. This follows the hosted baseline's finding that substantial
work occurs before generation. It establishes no hosted latency improvement.

## Ownership probe

The queue's ownership check previously reconstructed bounded conversation history to compare
the run revision. It now selects revision metadata. Generation, conversation ownership/deletion,
run and job locks, ownership token and database-time expiry checks remain in the same order.
No external call or output policy changes. The journal is still read where context is needed.

A balanced before/after/after/before sequence used the same live lease, four-connection pool,
16 retained synthetic turns and real local PostgreSQL. Each probe discarded 20 warmups and
retained 1,000 samples. The valid-owner result was checked on every iteration. The original
implementation is retained in the ignored `scratch/group12/bench-ownership-balanced.py` probe.

| Sequence | Median | Nearest-rank p95 |
| --- | ---: | ---: |
| Before, first | 3.04 ms | 4.93 ms |
| After, first | 0.96 ms | 2.06 ms |
| After, second | 0.94 ms | 1.47 ms |
| Before, second | 2.06 ms | 3.08 ms |

The difference removes transcript reads/decoding from a repeated metadata boundary. This
internal probe cannot be substituted for complete-turn latency or the PERF-002 target.

## Full local cohorts

Both configurations use the metadata optimisation, one API process, pool size four per process,
real loopback HTTP/PostgreSQL 17.11/Redis 7.4.12 and a synthetic 120-ms streaming model.
Each tier schedules 100 attempts at 120/minute, with one separate warmup. Twenty ten-session
stream-cap rejections in each configuration recovered the original durable answer without
resubmitting input. All accepted runs eventually completed correctly.

| Workers / total slots | Sessions | First-pass correct | Client backlog | Final p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| 1 / 4 | 1 | 72 | 28 | 278 / 789 ms |
| 1 / 4 | 2 | 100 | 0 | 276 / 351 ms |
| 1 / 4 | 5 | 100 | 0 | 347 / 635 ms |
| 1 / 4 | 10 | 80 | 0 | 565 / 764 ms |
| 2 / 8 | 1 | 86 | 14 | 272 / 566 ms |
| 2 / 8 | 2 | 100 | 0 | 278 / 294 ms |
| 2 / 8 | 5 | 100 | 0 | 300 / 357 ms |
| 2 / 8 | 10 | 80 | 0 | 376 / 440 ms |

Increasing capacity helped the five/ten-session tiers in this pair. The single-session tier
still misses offered arrivals. The earlier [matched-capacity record][matched] compares one
and two processes at four total slots; this additional pair changes aggregate capacity too.
The new four-slot full workload did not improve consistently over that historical run, despite
the faster isolated ownership check. Shared-workstation scheduling and one measured pair limit
causal claims. No percentile combines tiers or includes recovered turns as first-pass successes.

Five-second process samples are included in the JSON record. All five processes ended with
four available pool connections and zero waiting requests. Final asyncio task counts returned
to three in each API and five in each worker. Peak sampled RSS was 172.1–172.5 MiB; final RSS
was 49.2–144.5 MiB. Sampling can miss brief peaks and does not establish a long-duration leak
bound. Cumulative pool wait was 7.42 seconds in the four-slot worker and 2.54/2.79 seconds in
the eight-slot workers; these totals cover different request counts and are not wait percentiles.

An additional statement-count probe drove ten fresh queued turns through the pilot composition,
with one warmup, a fixed local prompt and a synthetic `READY` provider. Each turn used 182
explicit SQL statements: 21 admission, 18 preparation claim, 48 preparation execution,
18 inference claim and 77 inference execution. This excludes HTTP/SSE polling, setup and implicit
transaction commands. Twenty-six generation checks, twenty-two conversation-head reads and
fifteen job reads were among those calls. They enforce locking and execution invariants;
the count alone cannot justify removing checks or establish Azure per-statement latency.
The ignored `scratch/group12/profile-roundtrips.py` retains the probe; safe counts are in JSON.

Executed sequentially, with real service DSNs supplied:

```sh
uv run python -m tests.scaling.conversations \
  --output scratch/group12/metadata-one-worker --workers 1 --slots 4 --count 100 --interval 0.5
uv run python -m tests.scaling.conversations \
  --output scratch/group12/increased-two-workers --workers 2 --slots 4 --capacity 8 \
  --count 100 --interval 0.5
```

Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84` plus local group-12 changes.
The running processes predate the subsequent SSE backend-failure wakeup patch. These are
synthetic local capacity observations; the hosted replica configuration was unchanged.

[matched]: 2026-11-04-matched-worker-capacity.md
