# Two-hour conversation soak

This operator harness is prepared for group 12. Its unit tests use an injected clock;
they establish harness behaviour only. No hosted two-hour result is claimed here.
Read the root guidance, [performance rules](../../performance/AGENTS.md) and
[deployment guide](../../infra/azure/LIVE.md) before running it.

## Workload and bounds

The default plan creates two fresh conversations under the authenticated test user:

1. Seed different fictional verification codes, then discuss the reviewed
   [synthetic dialogue](dialogue.json) for 20 minutes.
2. Leave both conversations passive for at least 65 minutes. One keeps an SSE reader;
   the other has no stream. Both receive metadata-only inspection every minute.
   There are no message admissions or activity signals during this phase.
3. Ask each conversation to recall its own code, then resume the dialogue until at
   least two hours have elapsed. Normal timing leaves about 35 minutes for this phase.
   Finish with exact code recall in both conversations after the remaining compactions.

Active phases alternate conversations with at least 45 seconds between completed turns.
This is an endurance workload, with no saturation or percentile claim. The hard admission
cap is 100 attempts, including seeds and recall. Any failed/uncertain turn stops new admissions;
its run ID was saved before POST and can be inspected without submitting a duplicate.
Individual turns have a 120-second polling deadline and bounded HTTP timeouts. Accepted work
may continue durably after a client timeout, operator stop or process interruption.

Answer checks scan `/history`'s bounded recent complete-turn view and require exactly one
assistant answer for the admitted run ID. This endpoint retains the latest complete turns;
it is separate from the 100-event journal pagination interface. Matching old answers cannot
pass a missing current recall. The idle stream resumes from its last durable SSE cursor
across reauthorization and never calls `/history` or `/activity` during the passive interval.

The campaign also requires at most 1,000 provider calls, two million input tokens and
150,000 output tokens across all group-12 runs. Allocate a distinct share to this soak through
the three required `--budget-*` arguments. Shares allocated to all cohorts together must fit
the remaining campaign budget. This runner supervises only its two new conversations.

Before each admission, the runner reads scoped `inspect.usage` counters from both conversations
and reserves headroom for one accepted turn. The default reservation is 100 calls, 819,200 input
tokens and 102,400 output tokens. These conservative bounds follow the current pilot's shared
100 worker-attempt ceiling, 8,192-token context window and 1,024-token maximum summary output.
Each inference claim, summary reservation and explicit context-overflow retry charges that
shared attempt counter. Preparation/tool attempts also consume it. The code boundaries are
`QueuedCoordinator.initial`, `PostgresQueue.claim`, `SemanticStore.reserve` and
`ContextRecovery.claim`.

A lower `--reserve-calls` value requires a verified matching trusted runtime limit before
execution. Changing the client flag alone does not constrain accepted work. Review the deployed
profile whenever those limits change. If the allocated share cannot fund the next reservation,
the runner stops before POST. Sufficient samples and the complete elapsed interval remain
required for acceptance; an early budget stop cannot pass the endurance gate.

After a terminal turn, metadata-only reads allow up to five seconds for the bounded usage writer
to catch up. Missing counters, nonzero `unknown_usage`, active work or observed calls below
`expected_calls_lower_bound` stop further admissions. That lower bound includes completed model
steps and published summaries. Counter regressions and observed reservation overruns also stop
the run. A pending/unknown turn keeps its outstanding reservation in the evidence ledger.
The ledger records actual observed usage separately from conservative reservations.

The lower-bound check cannot detect every missing failed-call observation. Correlated telemetry
loss/error checks remain required before claiming complete accounting. This scoped supervision
does not establish a dollar spending guarantee or replace campaign-wide allocation and review.

The runner requires all of these lifecycle checks:

- Actual elapsed duration of at least two hours, with at least 65 minutes of passive idle.
- Both Redis history views expire while PostgreSQL cursor, run and journal counts stay stable.
- The idle SSE reader remains connected for at least 95% of the idle interval without errors.
  Scheduled reconnects within ten minutes refresh authentication and retain the durable SSE
  cursor. The API's five-minute `reauthorize` event is a planned reconnect. A connection also
  ends earlier when its current credential approaches expiry. Unexpected EOF remains a failure.
- Both conversations recall their own original verification code after idle and on the last turn.
- Each conversation has published at least five durable summary versions by the end.

The summary gate requires `GET /inspect` to return `memory.summary_versions`. Older images
leave it explicitly unmeasured and fail that gate. A dialogue that triggers fewer compactions
also fails; do not rewrite the threshold or count cached reads as compactions to make it pass.
Inspection checks the configured Redis TTL before idle; a TTL too long for this plan stops
the run. The runner never changes cache TTLs, deletes cache keys or changes infrastructure.

## Reviewed fixture sizing

Each ordinary active turn repeats a disposable fictional exhibition example, followed by a
request for exactly `READY`. It contains no persistent project decisions or assignments.
The fixed canary decision seeded in each conversation supplies the durable fact to preserve.
Using the deployed model's `o200k_base` tokenizer, the example is 6,007 UTF-8 bytes and 1,050
text tokens; a one-message request with accounting overhead is 1,400 tokens. All 39 extracted
candidates have the ordinary `fact` label, so examples do not accumulate protected facts.

The reproducible offline probe uses the current prompt catalog, pilot tool schemas, actual
context assembly and compaction selection, 8,192-token budgets, two retained turns and the
20% trigger / 40% free-space target. Its synthetic selector retains the canary decision plus
either zero or four ordinary excerpts. It assumes short replies and no retrieval/retry calls.

```sh
PYTHONPATH=. uv run python -m tests.soak.sizing
```

On 4 November 2026, 32 turns produced 13 or 18 projected summary versions per canary; 39 turns
produced 17 or 22. The zero-latency schedule admits at most 80 turns across both conversations
(41 and 39, including seeds and both recall pairs). That pair projects 557,115–566,605 input
tokens, 909–3,370 output tokens and 115–126 provider calls. The input totals include the
token counter's framing allowance. They are planning estimates, with no provider billing,
elapsed endurance or live summary-quality claim.

The selected soak allocation is 800 calls / 1,500,000 input / 140,000 output tokens. After
releasing each settled reservation, the next admission requires observed usage at most
700 calls / 680,800 input / 37,600 output tokens to retain full worst-case per-turn headroom.
The projections fit that margin. Larger summaries, retrieval, retries or slower turns can
still stop the actual run or reduce its sample count; only actual published summaries and
reconciled usage decide acceptance. The five-summary threshold remains unchanged.

## Selected identity

Group 12 uses the existing operator's delegated Azure CLI login with multiple fresh,
isolated conversations. The user selected this path on 4 November 2026. No new Entra user,
allowlist change or browser sign-in is required for this soak. Existing chats remain intact.

The current pilot accepts delegated user tokens from its configured tenant, audience,
client allowlist and user object-ID allowlist, with `runtime.invoke`. Terraform currently
supplies only the operator ID to `ALLOWED_USERS`. A managed identity or newly created service
principal cannot substitute for an approved delegated user under that policy.

Omit `--azure-config-dir` to use that current login. The runner's `--tenant` and `--user`
arguments bind the operator's immutable IDs; a different login is rejected locally, and the
API still performs authoritative token validation. The harness never writes bearer tokens.
Azure CLI manages its existing credential cache. The harness creates no identities and makes
no login-state changes.

This same-user result establishes conversation isolation and lifecycle behaviour. Validation
between distinct hosted Entra users remains a separate gap; synthetic local cross-user tests
cannot establish real directory onboarding or revocation. A later approved second-user exercise
would require a delegated test user, Terraform-managed allowlisting and that user's sign-in.
The optional `--azure-config-dir` argument remains available for its separate CLI profile.

## Execute after release and budget allocation

Confirm that the inspected deployed image matches the intended group-12 release and that
its required service checks pass. Use fresh output/stop paths; the source client config is
read-only and its existing conversation/run are ignored. This command starts model traffic:

```sh
PYTHONPATH=. uv run python performance/workloads/soak.py \
  --config scratch/live-client.json \
  --output scratch/group12-soak/observations.json \
  --stop scratch/group12-soak/STOP \
  --dialogue tests/soak/dialogue.json \
  --tenant '<existing-tenant-id>' --user '<existing-operator-object-id>' \
  --image-digest 'sha256:<inspected-64-character-image-hash>' \
  --budget-calls '<allocated-calls>' \
  --budget-input-tokens '<allocated-input-tokens>' \
  --budget-output-tokens '<allocated-output-tokens>'
```

The command refuses to overwrite an existing output. Leave its process and host awake for
the full duration. A supervising agent can inspect the growing JSON without sending model
messages. Create the stop file to prevent further admissions; in-flight work finishes or
reaches the bounded client deadline. Stop never deletes accepted work or changes its budgets.
Hosted traffic remains pending the parent agent's tested release and workload authorization.

```sh
touch scratch/group12-soak/STOP
```

After a stop or crash, inspect the last saved conversation/run before any fresh run. The harness
does not automatically resume a partially completed soak or re-admit an uncertain request.
No result can satisfy the endurance gate without the full real elapsed interval.

## Evidence and remaining measurements

The ledger preserves every admission attempt, source commit/dirty state, supplied deployed
image digest, UTC boundaries, monotonic durations, HTTP/status/error codes, cursor and count
observations, Redis TTLs, summary versions, token refresh counts and aggregate SSE counts.
It also records actual scoped call/token usage, completeness checks and outstanding reservations.
Messages, model answers, tool/report payloads, bearer tokens and raw exception strings are
excluded. Dialogue hashes identify the input fixture; only in-memory answer comparisons occur.

Periodic CPU/RSS, database connections, pool/lock waits, worker counts and logging loss need
separate correlated backend collection. The ledger flags those limits even when its lifecycle
checks pass. Join those records before concluding that there is no resource growth.
Model-text streaming latency, slow consumers, concurrent throughput,
cross-user isolation, rollout and memory semantic scoring remain separate group-12 cohorts.

Run the focused local harness checks without cloud calls:

```sh
uv run pytest tests/soak -q
uv run basedpyright tests/soak performance/workloads/soak.py
```

The full repository gate remains the parent implementation's responsibility. Short synthetic
clock tests must never be recorded as the hosted two-hour observation.

Executed on 4 November 2026: the initial 26 focused tests passed in 0.31 seconds; the usage-ledger
extension passed 41 focused tests in 3.59 seconds. The larger fixture, sizing and final recall
extension passed 45 focused tests in 5.42 seconds. Targeted Ruff, formatting, types, readability
and 100-column checks passed, as did CLI help and `git diff --check`.
The late-history and passive reconnect review passed 48 focused tests in 8.18 seconds;
this includes current-run recall after 300 previous events and durable cursor reuse.
These checks made no hosted calls and created no identities or cloud resources.
