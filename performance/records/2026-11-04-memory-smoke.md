# Preliminary live memory check, 4 November 2026

The `english-amount-correction` fixture passed twice against the live Foundry model. Both
answers recovered the corrected amount/currency after real compaction, with no unnecessary
history retrieval. This is one of the twenty scenarios; it does not qualify the full suite.

[Safe measurements](2026-11-04-memory-smoke.json) retain actual numeric usage and publication
boundaries. Seven model calls used 24,430 input and 155 output tokens, with no unknown usage.
Three summary generations took 1.163, 2.424 and 2.556 seconds including private-relay overhead.
Their separate local publication transactions took 6.5, 12.1 and 13.5 ms. No percentile is claimed.
One repetition needed two summary publications, so it is not a one-compaction latency sample.

The evaluator seeded reviewed synthetic historical turns into an isolated loopback schema,
then used normal claims, leases, memory selection and settlement. Actual selected histories,
summary facts and answers remain in ignored `scratch/group12/quality-live-amount.json` for
semantic review. Managed identity stayed in the temporary private relay helper. The hosted
app image, database, conversation scopes and ongoing soak were unchanged.

```sh
uv run python -m performance.workloads.memory_quality \
  --socket /tmp/agent-group12-quality.sock \
  --output scratch/group12/quality-live-amount.json --case english-amount-correction \
  --budget-calls 12 --budget-input-tokens 60000 --budget-output-tokens 1500 --dispatch-interval 0
```

The standard disposable PostgreSQL DSN on port 55449 was supplied. Start/stop the relay using
the [Azure validation guide](../../tests/azure/README.md); it consumes no inference until a
request arrives. The helper was stopped after collection and the owned schema was removed.
Keep this local-state/live-model result separate from hosted HTTP conversation latency.

A separate local negative control completed five compactions in each of two repetitions, then
guessed the expected answer without retrieving the omitted source. The evaluator correctly
rejected both. This validates the acceptance gate; the control made zero live model calls.
Its raw evidence remains in `scratch/group12/quality-omission-control.json`.
Another two five-compaction controls completed a real local `retrieve_history` call for the
wrong source page, then guessed the correct answer. Both were rejected because the required
fact never reached the answer model through the retrieved evidence. These controls also used
zero live calls; raw results are in `scratch/group12/quality-wrong-page-control.json`.
