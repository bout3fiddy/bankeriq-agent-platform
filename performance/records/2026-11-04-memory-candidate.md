# Memory candidate validation, 4 November 2026

Seven scenarios passed both repetitions against the live Foundry model using local memory
profile v9 and system prompt 1.2.0. Independent review confirmed identities, current/historical
amounts, corrections and resolved-language state. All 28 summary calls in this subset completed
in 0.919–5.480 seconds, median 1.446 seconds; local publication took at most 15.680 ms.
The required twenty-scenario suite remains incomplete. These are generation round trips through
the test relay, including its transport overhead; they establish no hosted conversation latency
or 100-sample compaction percentile. [Safe evidence](2026-11-04-memory-candidate.json) preserves
all candidate attempts, including earlier failures, rather than pooling only successful repeats.

## Implemented changes and review

An empty summary now advances coverage after protected decisions, constraints, identities and
tool states are restored. Original journal history remains available. Selection guidance asks
for lasting relevant information and explicitly omits disposable examples and prior one-turn
reply instructions. A new memory fingerprint prevents incompatible prepared work from using
this policy. Response guidance 1.1.0/system 1.2.0 retains earlier immutable versions.

The targeted approval case passed twice with empty summaries and correct false approval state.
The Dutch payment claim still exposed two issues: one repetition retained old reply instructions;
another returned the correct false state in JSON with surrounding prose, failing the strict
answer-format check. Both remain in the candidate evidence. General semantic relevance and
requested-format adherence are therefore not fully solved by the new guidance.

The later isolated cases passed English and Dutch distinct identities, mixed owner correction,
English and Dutch amount/currency correction, historical-versus-current amounts and mixed
resolved languages, twice each. Summaries retained the relevant facts without disposable
examples. Some model retirement suggestions referred to the same old/new candidate; trusted
validation rejected those suggestions and retained the correct current fact. No unsupported
retirement was published. The original-detail retrieval, action-state and remaining scenarios
still need their full v9 repetitions; earlier v8 retrieval evidence remains separate.

## Transport failures and recovery

A full v9 attempt stopped after seven completed cases and one interrupted case when the private
exec channel reset. Its unknown attempt retains 3,344 input/1,024 output tokens. An isolated
rerun then completed six scenarios twice before Azure rejected the next exec WebSocket handshake
with HTTP 429. That rejection preceded model dispatch and incurred no model-call allocation.
It came from the Container Apps test-console endpoint. A later connection succeeded after a
cooldown; the seventh scenario then passed twice. No credential change or cloud provisioning
was needed. The exact console service limit was not established.

The relay now rotates after two minutes between fully received responses and waits up to five
seconds for the helper's BYE acknowledgement before closing its terminal. It never resends an
uncertain model request. Real acknowledgement was observed at 14:47:52.763 and 14:51:31.842 UTC;
the local socket was removed. Older disconnected helpers retain their bounded alarm; their
immediate remote termination was not established. Test helper activity is separate from the
runtime's process-resource qualification.

## Source, commands and limits

Source HEAD was `54440b894deefad6dbfefe70252bfab37013fa84` plus local changes. All candidate
cases used memory v9/system 1.2.0. The final recovered-channel pair additionally used the local
PostgreSQL read-batching change; summary instructions and profile were identical. Azure remains
on revision 8 with profile v8/system 1.1.0. Its full image digest is in the safe record
and [deployment guide](../../infra/azure/LIVE.md). No candidate upgrade is claimed.

Each case owns and removes a temporary real local PostgreSQL schema. Historical exchanges are
synthetic seeds, while summaries and final answers use the live model. Ten-second test dispatch
spacing is outside each generation timer. A representative command, with an allocated budget:

```sh
BANKERIQ_TEST_POSTGRES_DSN='<disposable-loopback-primary>' \
  uv run python -m performance.workloads.memory_quality \
  --socket /tmp/agent-quality.sock --output scratch/new-owner-case.json \
  --case mixed-owner-correction --budget-calls 14 \
  --budget-input-tokens 40000 --budget-output-tokens 6000 --dispatch-interval 10
```

Executed wrappers, failed logs and full synthetic transcripts remain in `scratch/group12/` and
ignored `build-order/evidence/group12/`. The latest subset comprises `quality-v9-isolated/`
plus `quality-recovered-channel.json`. Earlier `quality-v9.json` and `quality-fix-*.json` remain
separate collections. Unknown reservations are included in `current-stage.json` campaign totals.
No unexecuted or interrupted scenario counts as a passed gate.
