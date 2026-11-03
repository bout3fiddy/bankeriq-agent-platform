# Local workload and acceptance limits

Declared before the optimized measurement, 3 November 2026. These are synthetic local
regression limits; they are not production latency or availability guarantees.

- Progress: 30 measured updates after five warmups at 10, 300 and 3,000 retained turns.
  Each seeded old turn has two 512-byte messages. Trace Python allocations consistently
  in both runs. Require zero history reconstruction calls per steady progress poll,
  median below 100 ms and a large/small median ratio below 2.0. Compare process CPU,
  retained/peak Python allocation and Redis traffic. Retain the baseline unchanged.
- Sustained execution: two API, two coordinator and two tool processes; four active slots
  per queue globally, two per process and two per tenant. Admit 400 conversations/turns,
  alternating reused and new conversations across two tenants, with three pure tools per
  turn (2,800 durable jobs). Fixed input/output sizes and synthetic model/tool delays.
  Record early/late windows, process RSS/CPU, lease peaks, queue age, admission/commit
  latency, warm/cold reads, first disposable model-token latency and projection lag.
- Require all 400 turns to finish, one join per turn, no duplicate outcomes, both tenants
  progressing in both windows, and no queue exceeding four leased jobs. Worker RSS after
  drain must stay within 64 MiB of its warmed early sample. After drain, zero execution
  tasks and zero live claims must remain. Redis progress stays at or below 32 entries;
  idle TTL/cleanup removes the tested cache keys.
- Claims: retain EXPLAIN ANALYZE/BUFFERS plans before and after thousands of completed
  jobs. Due-work selection must use a partial eligible-job index and must not scan
  completed job payloads. Record rows visited and buffer usage. Closed-history growth
  must not make a progress poll reconstruct a transcript.
- Recovery: process-kill gates at admission, claim, model response, effect, child settlement,
  join and final persistence; existing cancellation/unknown-commit gates also run. Restore
  a real local logical backup into a fresh database with dispatch paused, advance the
  generation, reconcile the fixture's external effect/acknowledgement ledger, and only
  then resume. Cloud HA and optional replica routing remain separate conditional gates.

Retention policy/physical deletion and quotas belong to group 13. This group verifies
that current idle/cache cleanup retains durable deduplication and reconciliation evidence.

The initial unpooled load attempt lost host-side connections while PostgreSQL remained
healthy inside Docker. The accepted workload opts into four primary connections per
process (including the sampling process), with at most eight waiting borrowers and a
one-second acquisition deadline. The database budget remains 56. This configuration
was selected before the successful 400-turn run; no throughput threshold was relaxed.
