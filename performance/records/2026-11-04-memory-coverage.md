# Empty-summary coverage and full memory suite, 4 November 2026

All twenty revised scenarios passed both repetitions against the live private Foundry model.
All 74 expected values were correct, including identities, corrections, currencies, uncertainty
and tool/action state. Four omitted-detail questions automatically used authorised retrieval
and answered from the returned original source. Both long scenarios underwent six compactions
per repetition. [Safe measurements](2026-11-04-memory-coverage.json) retain every case and timing.

## Implemented fix and fixture boundaries

An earlier v9 repetition returned an empty answer after five empty summaries. Context assembly
had dropped the memory message entirely when the summary contained no selected facts. Profile
v10 now includes a bounded user-role coverage marker in that situation: earlier history exists
and may contain the requested detail. Token planning includes this marker. It supplies no
original text, tool permission or new authority. Scoped retrieval remains the source of evidence.

Seventeen fixtures are unchanged. Two original lone-detail cases correctly remembered their
answers and therefore failed to exercise retrieval. Their replacements use 48 incidental codes,
exceeding the selector's 24-entry ceiling; the runner selects a predeclared omitted detail and
then proves its absence again in the actual question input. The date case now explicitly asks
for the date agreed in the conversation. The earlier ambiguous calendar question produced
invented dates despite a retained correction. Calendar grounding remains a known limitation.
The original fixture file and failed/unexercised results are preserved separately.

Independent review checked source fidelity, correction meaning, action state and relevance.
All selected source text and current answers passed fidelity checks. Both mixed retrieval
repetitions retained one incidental catalogue description in their first summary, then dropped
it. These two relevance exceptions remain recorded. A quoted historical amount alongside its
explicit correction is retained history; the final answers correctly distinguish current values.
User assertions did not become verified approvals or completed payments. The calculation case
executed the real synthetic tool and retained its permitted completed result.

## Summary timing

The suite generated and published 96 summaries. Sixty later calls include direct remote SDK
measurements: minimum 0.519 seconds, median 1.080 seconds, maximum 2.526 seconds. These cover
provider transport, SDK parsing and any identity refresh; they do not isolate model-server
compute. Every one meets the ten-second boundary. Local PostgreSQL publication took at most
22.882 ms across all 96 publications. This diverse fixture set establishes no compaction p95.

All original local round trips remain recorded, ranging from 0.700 to 11.055 seconds. Two early
long-case calls exceeded ten seconds during operator-channel rotation and have no remote timing.
They remain unmeasured at that boundary. Later validation headers measure channel setup and
remote SDK duration separately; no estimated reconnect time is subtracted from older records.
The summary-speed gate has sixty directly timed passing samples, independently of those gaps.

## Reproduction and environment

The suite used local isolated PostgreSQL schemas, real durable queues, live Foundry
`gpt-4o-2024-11-20` in Sweden Central and seven seconds between dispatch starts. Histories were
explicitly synthetic seeds. The model used system 1.2.0 and memory profile v10; Azure's serving
runtime still used revision 8/profile v8 during this quality run. This establishes memory
behaviour and summary timing, with hosted mixed-load latency qualification still pending.

Eleven cases completed before Azure's Container Apps exec endpoint rejected the next connection
with HTTP 429. It preceded model dispatch and added no model usage. After a cooldown, the nine
remaining cases completed through one rotating relay. All 200 model calls have known usage:
672,066 input tokens and 3,483 output tokens. Earlier unknown campaign reservations remain held.
Raw transcripts, review notes and wrappers are in `scratch/group12/quality-v10/` and
`scratch/group12/quality-v10-remaining/`, copied into ignored build-order evidence.

Each case used this command shape, with remaining allocation supplied from the approved ledger:

```sh
BANKERIQ_TEST_POSTGRES_DSN='<disposable-loopback-primary>' \
  uv run python -m performance.workloads.memory_quality \
  --socket /tmp/agent-group12-quality.sock --output scratch/new-quality-case.json \
  --case mixed-owner-correction --dispatch-interval 7 \
  --budget-calls 70 --budget-input-tokens 200000 --budget-output-tokens 15000
```

The safe record preserves both fixture hashes, the runtime profile hash and raw collection
hashes. Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84` plus local changes. Full
verification passed 707 tests, zero skips, in 137.44 seconds with real local PostgreSQL/Redis;
`git diff --check` passed. The new process test kills a worker after actual text publication,
rejects its expired draft, resumes on a fresh worker and recovers exactly one durable final
through a separate API process after scoped cache loss. Hosted release/latency gates remain open.
