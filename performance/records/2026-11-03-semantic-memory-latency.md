# Semantic-memory latency investigation: 3 November 2026

Turn 6's minute-long response is explained by the application-owned quota limiter.
The same limiter produces the repeated roughly 65-second cycles elsewhere in the
[hosted transcript](../../build-order/evidence/11-hosted-live-model-transcript.md).
The [measurements](2026-11-03-semantic-memory-latency.json) retain client timings, worker
spans and reconstructed reservation estimates without conversation payloads.

This is a passive investigation of the recorded 18-turn run. No new inference, deployment,
configuration change or runtime mutation was performed. Azure Log Analytics supplied 186
metadata events for this conversation, including later passive cache inspections.
The image is the semantic-memory release based on uncommitted changes after commit
`0be1b179ab4b81c4dee42115f792e6db3993be60`; its digest is retained in the JSON.

## Turn 6

The client recorded 60.578 seconds end to end and 0.172 seconds for admission. Logs show:

| Stage | Seconds |
| --- | ---: |
| Accepted to preparation claim | 0.184 |
| Preparation worker | 0.130 |
| Preparation settled to inference claim | 0.052 |
| Inference worker, including quota waiting | 59.640 |
| Inference settled to history cache read | 0.375 |
| Cache reconstruction | 0.029 |
| Remaining client/transport residual | 0.168 |

Admission overlaps these stages; do not add its 0.172 seconds again. Cross-process clocks
are assumed aligned, and rounded client timing does not provide microsecond accuracy.
There was one inference attempt, no tool call and no summary operation on this turn.
Compaction first ran on turn 17. The inference span accounts for about 98.5% of turn 6.

The original `PacedModel` (removed by the subsequent latency fix) counted estimated request tokens
plus the configured
maximum output, reserves that amount for 65 seconds and waits when the rolling total would
exceed 8,500. It does not release unused output reservation after a shorter answer.
The quota wait occurs before the provider call. Both normal inference and summary generation
use this same limiter in [pilot startup](../../tests/azure/pilot.py).

Offline reconstruction using the saved events, unchanged prompt catalogue, tool schemas and
same tokenizer gives these reservations, including 512 output tokens per conversation call:

| Turn | Reconstructed reserved tokens |
| --- | ---: |
| 4 | 3,096 |
| 5 | 3,321 |
| 6 | 3,530 |
| Combined | 9,947 |

These are estimates produced by our counter, not measured provider usage. Turn 6 cannot share
that allowance with turns 4 and 5, so it waits for older reservations to expire. The exact
wait/provider-time split was not instrumented. The code, estimates and timing pattern support
quota waiting as the dominant cause; the 59.640-second worker span is not pure model latency.

## Other turns

| Turns | Client time | Explanation supported by code and records |
| --- | --- | --- |
| 1–3, 5, 7 | 3.586–4.459 s | Small calls fit available reservation capacity. |
| 4 | 59.997 s | Existing reservations prevent immediate dispatch. |
| 8 | 63.740 s | Inference spans 58.347 s and 3.701 s; calculator worker 0.140 s. |
| 9–16 | 61.297–65.856 s | Growing context permits roughly one call per window. |
| 17 | 129.700 s | Summary and answer compete for the same allowance. |
| 18 | 126.596 s | Tool-request and final-answer inference each take another quota slot. |

Turns 1–3 reserve 7,811 tokens together. When turn 4 arrives, turns 2–4 alone would reserve
8,549, so expiration of turn 1 alone is insufficient. By turns 9 and 10, adjacent calls reserve
4,259 + 4,453 = 8,712 tokens: they cannot share an 8,500-token window. Turn 16 reaches 8,085.
This is why the increasing context eventually gives almost every turn a minute-long delay.

Turn 17 reconstructs as a 7,767-token summary reservation followed by a 6,267-token answer
reservation. The earlier turn still occupies capacity when the summary starts waiting, and
summary plus answer also cannot fit together. One summary succeeded on attempt one.

Turn 18 reconstructs as 6,469 and 7,045 tokens for its two inference requests. Their observed
worker spans are 61.100 and 63.654 seconds. The two history jobs reported 0.292 and 0.319 seconds;
their overlapping group occupied about 0.373 seconds from first claim to final settlement.
They shared one join continuation. The extra history read did not create a third inference step.
All recorded jobs completed on attempt one; there is no observed retry loop behind these delays.

## Answer and summary quality

- Turn 17's summary incorrectly labels the already-answered tools-description request as open.
- It omits the accessibility reviewer, prototype name and calculator's numeric result. Recent
  complete turns, particularly turn 16's detailed checkpoint, help the subsequent recall.
  Correct recall therefore does not establish that the summary alone retained those facts.
- Turn 18 correctly retrieves source 1 and the omitted `LANTERN-7B` label. Its second read,
  `after=54, limit=1`, returns its own current question at source 55 and contributes no budget
  evidence. This is redundant tool selection, although execution remains bounded and scoped.
- Source quotations and action-state origins are preserved. Semantic relevance and labels
  remain model judgments; this fixture establishes useful retrieval with visible quality limits.

## Implications and next measurements

The conservative limiter was introduced during live validation and wired into the deployed
profile. It made this functional test predictable while imposing poor interactive latency.
An idle interval over 65 seconds clears old reservations; a summary followed by an answer can
still require a quota wait within that one turn. The limiter is shared across conversations
in one coordinator process and does not coordinate separate replicas.

Measure provider-reported quota/usage before deciding the required allowance. This run did not
retain provider quota headers or actual input/output usage, so it cannot establish how much
waiting Azure itself required. Context budgets also include conservative framing headroom and
reserved output; they are a different quantity from actual consumption.

Add separate metadata timings for quota wait, provider request and context/summary work, with
estimated and actual token counts. This makes large worker spans attributable without exposing
messages. Then tune pacing, context size and capacity against the intended interactive workload.
Summary coverage and redundant retrieval need their own quality checks. No optimization or
capacity change is implemented by this analysis.

## Reproduce and validation

Client timing and exact source events are in the ignored task artifacts:
`scratch/group11-hosted-dialogue.json` and `scratch/group11-hosted-snapshot-cache-loss.json`.
The local reconstruction script is
`scratch/performance/group11-analysis/reconstruct.py`; run with
`uv run python < scratch/performance/group11-analysis/reconstruct.py` from the repository root.
It makes no model or database calls. The same source records and counter define the calculation.

The passive Azure query uses [recent-runs.kql](../queries/recent-runs.kql), additionally filtering
`tostring(e.conversation)` to `8e9071f9-a79f-4ed7-9da9-a5c7fa2a6fa4`. Retain only its projected
metadata. The current raw query result is `scratch/performance/group11-analysis/lifecycle.json`.
Logs have finite retention; the sanitized turn-6 events remain in the accompanying JSON.

JSON, line-length, local-link and `git diff --check` validation passed. No application code
changed, so the preceding 536-test runtime gate was not repeated. These mixed 18-turn samples
are outside the short-answer workload and minimum cohort size in the performance requirements;
no p50/p95 compliance or pure provider-latency claim follows from this investigation.
