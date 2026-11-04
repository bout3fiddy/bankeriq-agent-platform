# Live memory baseline, 4 November 2026

The first full group-12 memory campaign did not pass. It stopped after 13 of 40 planned
case/repetition pairs when the private operator relay disconnected. Eleven final-answer
checks passed; one Dutch answer ignored the requested JSON format; one case stopped before
its question. Independent summary review found irrelevant disposable examples in four cases.
[Safe evidence](2026-11-04-memory-baseline.json) records all attempted cases, numeric provider
observations, publication times, exact-check flags and reviewed relevance findings.

## Method and source

The evaluator used real local PostgreSQL 17.11 and the live private Foundry
`gpt-4o-2024-11-20` deployment, through the existing coordinator's managed identity. It owned
one temporary schema, removed on exit. Historical setup turns were explicitly synthetic seeds;
model replies, automatic compaction and history retrieval used the ordinary queued composition.
The 20 reviewed fixtures, repeated twice, are in `performance/workloads/memory_quality_cases.json`.

Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84` with uncommitted group-12 changes.
This baseline used immutable system prompt 1.1.0 and memory profile v8. Azure revision 8
provided private transport; local code owned execution/storage. It is a quality experiment,
with a ten-second dispatch interval outside generation timers, not hosted latency evidence.

The first nine cases preserved distinct English/Dutch identities, explicit corrections,
resolved questions and purchase constraints. The approval scenario correctly denied verified
approval, but selected up to 24 excerpts including disposable illustrations and old response
instructions. The Dutch payment claim correctly remained unverified, but its final prose
failed the required JSON check; three summaries also carried irrelevant details.

The original-ribbon case completed at least five publications and passed actual-omission and
retrieved-source checks. Its earlier summaries still retained disposable examples. One empty
model selection was rejected by the former validator, adding unnecessary work. Successful
recall does not excuse that relevance failure. The mixed original-label case stopped after
a relay reset; no final-answer or retrieval success is claimed for it.

## Failure accounting and fixes

The campaign retained 54 calls, 202,072 input and 3,378 output tokens including a conservative
reservation for the unknown attempt. That final attempt returned relay HTTP 503 in 7 ms after
`ClientConnectionResetError`; it supplied no provider usage observation. Its 3,433 input and
512 output reservation remains charged. These totals are not exact billing. Remaining cases
were never dispatched. The owned schema and local socket were removed; a remote helper alarm
bounds any disconnected helper's lifetime.

Subsequent local changes permit empty summaries after restoring protected facts, clarify that
24 selections are a maximum, and ask the model to omit disposable examples and past one-turn
instructions. Memory profile v9 prevents incompatible prepared jobs from changing semantics.
New response guidance 1.1.0 and system root 1.2.0 preserve prior immutable prompt files.
The validation relay rotates every five minutes between completed requests and never replays
an uncertain call. These changes require separate evidence; they do not rewrite this baseline.

## Reproduction

Start the private relay using the existing authorised coordinator resource, then run:

```sh
BANKERIQ_TEST_POSTGRES_DSN='<disposable-loopback-primary>' \
  uv run python -m performance.workloads.memory_quality \
  --socket /tmp/agent-quality.sock --output scratch/new-memory-quality.json \
  --budget-calls 250 --budget-input-tokens 750000 --budget-output-tokens 30000 \
  --dispatch-interval 10
```

Use a new output path and an allocation within the authorised finite campaign. The executable
returns a failure for incomplete repetitions, critical mismatches or missing required source
retrieval. Review every summary and final answer independently of those numeric gates.
Current source has the subsequent fixes; reproduce this baseline only with its original
v8 source/prompt snapshot. Raw synthetic transcripts and executed commands remain local in
`scratch/group12/quality-full.*`, `quality-review.json` and `run-live-quality.sh`, copied into
ignored `build-order/evidence/group12/`. No transcripts enter tracked telemetry/evidence.
