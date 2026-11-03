# Responsive conversations and automatic compaction

Collection date: 3 November 2026. Final hosted latency collection and reconciliation are complete.
The previous [latency investigation](2026-11-03-semantic-memory-latency.md) remains unchanged.
This report distinguishes implemented behavior, exploratory failures and final release evidence.

## Change and causal evidence

The earlier application reserved conservative context estimates against 8,500 tokens in a
65-second process-local window. A successful request could therefore wait nearly a minute
before contacting Azure. Turn 6 took 60.578 seconds; summary/tool-follow-up turns reached
126–130 seconds. That artificial admission wait is removed from the pilot and private relay.
One physical model request now receives a classified result. Actual HTTP 429 responses create
a bounded durable retry deadline and release the worker; existing attempt budgets still apply.
Unknown provider outcomes retain their previous recovery rules.

The existing Standard deployment increased from 10,000 to 50,000 TPM through Terraform.
Management reported the new capacity before inference headers changed. The first verified
50,000-TPM/300-RPM probe was approximately 22:06 UTC, after the 21:50 UTC apply. Sampling does
not determine the exact propagation instant. Azure documents [quota propagation and admission
estimation][quota]. This capacity remains a limit on sustained traffic, not a latency guarantee.
No new cloud resources were created.

Automatic compaction retains a target of two recent complete turns, triggers at 20% free
headroom and targets 40%. Main requests reserve 512 output tokens inside an 8,192-token
estimated window; summaries reserve 1,024 inside a separate 8,192-token window. Active original
input remains verbatim when completed exchanges within a long turn are compacted. Source-checked
structured facts, explicit replacements, bounded identity/confirmation preservation and one
durable context-overflow retry preserve the existing permission and execution boundaries.

## Final live summary cohort

The [safe measurements](2026-11-03-responsive-compaction.json) retain all 20 attempts from four
reviewed synthetic source fixtures, repeated five times each. They use the live private
`gpt-4o-2024-11-20` deployment through the operator relay and the normal summary request builder,
structured-output adapter and source validator. Fixture construction and authentication are
outside each measured call. Provider/relay transport and response parsing are included.

- 20 of 20 completed on their first attempt, without 429 responses.
- Duration range: **0.690–2.558 seconds**; all met the ten-second summary speed gate.
- All checked facts survived; no user claim became a completed tool action.
- Explicit simple budget/language corrections passed. Duplicate reviewer questions sometimes
  remained alongside their answer until later compaction supplied valid retirement advice.
  This conservative retention limit is recorded per sample and is not hidden as a resolved fact.
- This is a speed/correctness gate. Twenty samples establish no compaction percentile SLO.
- Token estimates and actual provider usage are separate fields. Every observation reported
  effective capacity of 50,000 TPM.

Reproduce after starting the existing private relay as described in the
[operator guide](../../tests/azure/README.md):

```sh
PYTHONPATH=. uv run python performance/workloads/summary_generations.py \
  --socket /tmp/bankeriq-semantic-relay.sock --output scratch/summary-samples-new.json
```

The source fixtures contain reviewed fictional conversation data. Raw generated selections stay
in ignored scratch output. The retained performance JSON includes only case names, timing,
numeric provider metadata and correctness counts.

## Exploratory findings retained

The first local unpaced dialogue observed 19 genuine rate-limit responses while Azure still
advertised 10,000 TPM during propagation. The runtime persisted bounded retries. Four later
turns failed when unsupported retirement advice rejected an otherwise valid summary. Subsequent
fixes ignore semantically unsupported retirement advice while preserving old protected facts;
invalid candidate identifiers and malformed shapes still fail. Another fix retains the active
user question across within-turn compaction, preventing a history-result page from displacing it.

A later 24-turn local trial completed with no throttle but confused the project name and
prototype nickname after omission from the summary. Its successor completed 25 turns, including
cache deletion and fresh worker processes, with correct checked recall. The five summary calls
took 1.984–3.096 seconds. These local timings include process startup, relay and polling.
See the [local transcript](../../build-order/evidence/11-local-automatic-compaction-transcript.md).

The [first hosted trial](../../build-order/evidence/11-hosted-automatic-compaction-initial.md)
completed operationally but returned the spare-box label as the prototype nickname on turns
23 and 25. It failed factual fidelity. Bounded retention now protects explicit user-supplied
names and ownership statements independently of selector omission. Rebuilding that older
conversation then exposed omission of a reviewer confirmation; explicit confirmation/correction
excerpts now receive the same protection. No assertion acquires tool-execution or approval
status through these categories. General semantic relevance remains model-dependent.

## Hosted workload and measurement boundaries

The final image was built from HEAD `0be1b179ab4b81c4dee42115f792e6db3993be60` plus the
then-uncommitted group-11 implementation. Its immutable digest is:

```text
sha256:842393e2bb8b82873541e62ef81b6cd15f148251a477de8db4fb78a793a7cc5c
```

The Sweden Central pilot has one fixed 0.5-CPU/1-GiB replica for API, coordinator and tools,
private PostgreSQL and Redis, two worker execution slots and 250-ms worker polling. No new
logging service was provisioned. Numeric `model.request`, `memory.assembled` and
`memory.compacted` events use the existing allowlisted console/Log Analytics path.

The fixed short-answer workload asks for exactly `READY`, with a fresh empty-history conversation
for each attempt. App/provider infrastructure is warm; each conversation view starts cold.
Conversation creation and Entra token acquisition are setup. Turn timing covers POST /runs
through receipt of final GET /history, with 50-ms status polling. One warmup is retained separately.
The one-worker cohort admits at most one message every three seconds. The two-worker cohort
submits paired messages, each worker starting at most every six seconds. Both offer at most
20 messages/minute. Interarrival spacing is outside turn timing and belongs only to this
benchmark. These are below-capacity workload measurements, not saturation throughput tests.

```sh
uv run python performance/workloads/short_answers.py \
  --config scratch/live-client.json --concurrency 1 --interval-seconds 3 --count 100 \
  --output scratch/short-serial-new.json
uv run python performance/workloads/short_answers.py \
  --config scratch/live-client.json --concurrency 2 --interval-seconds 6 --count 100 \
  --output scratch/short-paired-new.json
```

Run the cohorts sequentially after correctness traffic stops. Original client configuration is
read-only; every sample uses a separate synthetic conversation. Export numeric model observations
with `performance/queries/model-requests.kql` after ingestion, matching by run ID. Preserve all
failures and warmups. Use median p50 and nearest-rank p95, with at least 100 measured attempts
per cohort. Actual output token counts must confirm the short-answer workload boundary.

## Validation and limits

`uv run python scripts/verify.py` passed **574 tests in 136.17 seconds, zero skips**, with local
PostgreSQL 17.11, Redis 7.4.12 and independent worker processes. The DSN and reproduction commands
are in [group 11](../../build-order/11-completed-semantic-compaction-and-retrieval.md).
The suite covers exact tail coverage, headroom boundaries, pending approvals/jobs, cancellation,
worker restart, restricted-output isolation, retirement provenance, one-time overflow recovery
and durable real/injected throttling. Local fake transports exercise exact provider error codes;
the live propagation trial supplies actual Azure throttling evidence.

General banking-task quality, unrestricted entity resolution, production concurrency, provider
outages and saturation throughput remain unvalidated. A profile change can require multiple
summary chunks to rebuild an older long conversation; report that cold rebuild separately.
Protected facts can exceed configured bounds, producing an explicit failure with sources intact.
Compaction permits no journal deletion. First-token latency remains unmeasured with stream=False.
Application Insights, production metrics/alerts and storage lifecycle work remain separate items.

[quota]: https://learn.microsoft.com/azure/foundry/openai/how-to/quota

A final-rollout trial failed with `memory_profile_changed` while old and new queue consumers
overlapped. All old coordinator revisions subsequently reported zero replicas and `Stopped`.
This failed attempt is retained separately from the steady-state benchmark. It demonstrates
profile fencing and a release limitation: readiness of the new HTTP revision alone does not
establish that every old asynchronous consumer has exited.


## Qualified hosted results

Both cohorts retained 100 measured attempts plus one separate warmup. Every measured attempt
returned exactly `READY`, with one HTTP-200 model call, 1,121 actual input tokens and two actual
output tokens. All 202 provider observations, including warmups, were recovered from Log Analytics;
no console fallback was needed. There were no failed requests, retries or HTTP 429 responses.
All observations reported 50,000 TPM. No samples were removed.

| Measurement | Serial | Paired | Assessment |
| --- | ---: | ---: | --- |
| Complete response p50 | 1.237 s | 1.264 s | PERF-003 passes <= 2 s |
| Complete response p95 | 1.623 s | 1.651 s | PERF-003 passes <= 5 s |
| Admission p95 | 137 ms | 133 ms | PERF-001 passes <= 300 ms |
| Estimated non-model overhead p95 | 1.231 s | 1.207 s | PERF-002 misses <= 500 ms |
| Provider adapter duration p50 | 323 ms | 360 ms | Component observation |
| Provider adapter duration p95 | 454 ms | 532 ms | Component observation |

Warmups took 1.225 seconds (serial) and 1.294 seconds (paired). They remain in the JSON and are
excluded from the 100-attempt percentiles. Complete-response ranges were 0.943–1.858 seconds
and 0.997–2.105 seconds respectively. These results qualify only for the declared empty-history,
two-token response and offered-load envelope. They establish no saturation or production SLO.

PERF-002 subtracts the single observed adapter call from client turn duration. The remaining
interval includes application, queue, storage, network and delivery work. It is an estimate of
that combined boundary, not a pure server span. Detailed attribution needs follow-up profiling.
PERF-004 remains unmeasured: no exact durable-commit/client-delivery span was recorded.
PERF-005 remains unmeasured because the adapter does not stream model text.

## Final hosted recovery and recall

The [27-turn transcript](../../build-order/evidence/11-hosted-automatic-compaction-transcript.md)
contains every reply and all 11 durable summary versions. Turns 1–24 used the identity-preserving
image; turn 25 rebuilt memory under the final guarded-assertions profile. That five-chunk rebuild
took 11.369 seconds. The separate older exploratory conversation needed 27.923 seconds for its
full rebuild and correctly recovered current facts. These are upgrade-induced rebuilds, distinct
from steady-state compaction or Redis-view loss.

After deleting the final-profile conversation's Redis view, the durable cursor reconstructed
from 84 to 84 with all 11 summaries intact. Turn 26 correctly recalled the current CHF 1,800
budget, Copper Finch prototype, Mara/Ivo ownership, Noor, English/Dutch, undecided date/venue
and absence of purchases/bookings in **2.144 seconds**. It generated no new summary. Turn 27
retrieved the first user message through one tool child and one joined continuation, recovering
EUR 1,200, LANTERN-7B and Copper Finch in **3.907 seconds**. It also generated no new summary.

Turn 25 had called the original currency unclear without retrieving it. This is retained in the
transcript: automatic compaction does not guarantee that the model always chooses retrieval.
An explicit bounded retrieval request recovered the source detail on turn 27. The final active
summary has no unresolved reviewer/language questions; validated retirements keep source links.

The final transcript scope has 27 runs, 62 journal events, cursor 93, 11 summary versions and
four joined tool groups. Every actual model/tool job completed on its first attempt. The original
user conversation remains at five runs, 14 journal events, 18 completed jobs and cursor 21; its
Redis view is expired. The 202 isolated benchmark conversations remain retained synthetic data.
Azure remains running on revision `--0000004`, with the reviewed final plan reporting no changes.

The task-owned local PostgreSQL/Redis containers were removed after validation using
`docker rm -f bankeriq-memory-pg bankeriq-memory-redis`. The retained Azure deployment was left
running. Recreate the disposable local services before repeating the real-service test suite.
