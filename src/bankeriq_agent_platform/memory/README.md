# Durable semantic memory

The queued-runtime memory service compacts permitted conversation history into
source-linked excerpts and retrieves older details through the existing tool gateway.
Migrations 013 through 015 store versioned summaries, explicit prefix coverage, retirement links,
summary operations, retrieval charges and one context-overflow recovery per prepared step.
Queued startup requires this service. The synchronous in-memory profile is unchanged.

## Data and execution boundaries

`SemanticStore` reads the prepared model step's fixed journal cursor and active run's
admission sequence from PostgreSQL. Both boundaries matter: a queued turn can have an
earlier event cursor than the active turn's final answer. Summaries normally cover whole terminal
turns by admission sequence and also retain their maximum source event cursor. A partial-turn
summary marks its last covered event and retained user-input cursor. That input and the remaining
complete exchanges stay verbatim in the tail.
Tail reads exclude covered events and later queued inputs. The original journal is never deleted.

`SemanticMemory` assembles a compatible summary, recent complete turns and the active turn.
Structured memory is an explicitly untrusted user-role data message. Recent complete turns
retain permitted original message roles, exact text and complete tool-call/result pairs.
Trusted runtime/system instructions remain separate and keep the prepared prompt manifest.
`keep_recent_turns=2` is the default retention target; the full request budget can reduce it.
Before every model request, including tool continuations, automatic compaction starts when
free context falls below `reserve_tokens` (20% of the configured window by default). It aims
for `target_free_tokens` (40%), preserving a larger margin before another compaction is needed.
Instructions, tools, history and reserved output are counted before computing free space.
All settings are part of the prepared job's memory profile. More than 16 uncompacted turns
also triggers compaction to bound source reads. Irreducible recent input can prevent reaching
the target; an independently checked fitting request remains usable after a proactive failure.

The summarizer uses a strict JSON output schema to select numbered, precomputed excerpts
and descriptive labels. Trusted code splits source text at sentence/line boundaries, with
a 1,024-character fallback for long passages and at most 256 candidates per request. The
model receives a finite identifier enum; source cursors, source kinds and text are restored
from the selected candidates. It cannot generate replacement quotations or attributions. Its
full output schema is included in token accounting and the pinned memory profile. Trusted code
verifies every quotation and derives its origin and execution state from saved records.
User/assistant claims retain those origins. Approval and dispatch flags come from durable
approval/job records; an attempted action never establishes completion. Memory groups goals,
constraints, decisions, completed actions, unresolved questions and next
steps. Explicit user headings are protected even when the selector omits them. Prior selected
goals, constraints, decisions, questions, next steps and recorded action states remain protected.
The resulting
memory cannot approve work or replace the gateway, run budgets or execution records.
A validated explicit correction can retire a prior decision; an explicit answer can resolve a
question. Retirement requires later source chronology, an explicit replacement/resolution cue,
a shared subject/value and a permitted source kind. Trusted code records both source excerpts
and gives the validated replacement precedence. Ambiguous prose remains together for retrieval.
Unsupported semantic retirement suggestions leave the protected fact active and are not published.
Malformed identifiers and output shapes still fail validation. Prior versions and all original
sources remain available; omission never retires a fact.
A narrative overview is not enabled: exact source excerpts provide stronger attribution, while
free-form narrative introduces another unsupported-claim surface requiring separate evidence.

Each summary operation pins its own prompt manifest before inference and reserves one of
at most two attempts. Each physical summary request also consumes the existing run worker
budget. Database retries reuse a reservation identity. A crash after external inference can
require another model request; tools never execute from an uncommitted summary or reply.
Prepared coordinator jobs also pin the static memory format, instructions and token profiles.
An incompatible profile blocks that prepared job instead of changing its retry inputs.
Successful chunks survive restart. Provider failure terminates mandatory compaction with a
fixed reason; automatic process-loss recovery can reuse the remaining reserved attempts.

Publication commits content, coverage, provenance and operation completion in one transaction.
A version comparison and attempt token reject competing stale results. The same transaction
checks the actual inference worker lease, run revision, recovery UUID and deletion generation.
Duplicate publication reuses its version; conflicting content fails. Model/network calls happen
outside transactions. Existing cancellation, fencing and unknown-effect controls remain active.

## Access and output isolation

Startup supplies `access_for(context) -> HistoryPolicy`. Its revision must change when
visibility changes; `visible_tools` contains only currently permitted visible-output tools.
The `allows(fact)` callback rechecks source-level access, including already summarized excerpts.
For example, `lambda fact: fact.source not in revoked_cursors` removes those sources from memory.
Load external permissions in `access_for`; `allows` should evaluate that policy snapshot locally.
A policy revision change invalidates old summaries and blocks in-flight publication. Trusted
identity always comes from the run context, never retrieval arguments.

The projection allowlists known journal fields. It excludes provider reasoning, arbitrary
unknown records, restricted arguments/results, approval wait references and delivery identifiers.
Restricted operations contribute fixed state descriptions. No delivery table is read.
Past history-retrieval pages contribute a completion receipt; their nested source excerpts
are not reclassified as tool outcomes or repeatedly summarized. The active requesting turn
still receives the full enforced page with its original source kinds.
The historical data remains untrusted; extractive validation establishes provenance, while
semantic relevance and model obedience still require evaluation for a selected real model.

`retrieve_history` accepts only `after` and `limit`. Register `HISTORY_TOOL` and
`HistoryRetrieval(memory).binding()` in the ordinary registry/gateway, with `history.read`
permission and current resource access. Include `retrieve_history` in the history policy's
visible tools so its enforced result can reach the next model step. Configure its trusted child
binding and handler as replay-safe: reads carry no external effect, and duplicate delivery
reuses the durably charged page. The pilot declares replay permission for its pure fixtures
and retrieval binding. The normal tool-call budget and join continuation still apply.

Retrieval is capped at four distinct calls, 20 source events per page, approximately 8 KiB per
page and 32 KiB per run, with separate PostgreSQL row bounds. Pages return source cursors and
a continuation cursor. An individual excerpt exceeding the page bound fails explicitly.
Current policy, owner, active run and generations are checked before reuse. A new policy cannot
reuse an old retrieval receipt. Repeated calls still count as distinct calls when their stable
operation identities differ, even if arguments match.

## Composition

Application startup owns the async summarizer client, prompt resolver, permission source and
provider token counter. `compose_queued_runtime(..., memory=memory)` requires the service;
queued inference has no history-only fallback. The normal Azure application composition
always constructs it and registers the permission-checked `retrieve_history` tool.
Its context budget is 8,192 tokens with 512 reserved for output; its summary budget is
8,192 tokens with 1,024 reserved. No per-conversation enable flag is required.
The existing worker owns scheduling and lease renewal. No task or thread is held for an idle
conversation, and no model client or scheduler is introduced into core runtime logic.

A minimal construction, after obtaining application-owned dependencies:

```python
from agent_runtime.conversations.semantic import TokenBudget
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from bankeriq_agent_platform.memory.service import SemanticMemory
from bankeriq_agent_platform.memory.storage import SemanticStore

counter = FoundryTokenCounter("gpt-4o-2024-11-20")
memory = SemanticMemory(
    SemanticStore(store),
    summarizer,
    prompts=summary_prompts,
    summary_budget=TokenBudget("summary-profile-v1", 8192, 1024, counter),
    context_budget=TokenBudget("conversation-profile-v1", 8192, 1024, counter),
    access_for=access_for,
)
```

These are illustrative configured budgets, not a production capacity recommendation.
Use the actual model identity for tokenization; unknown identities fail at startup. Both
budgets include resolved instructions, all source/history text, tool schemas and reserved
completion tokens. The request carries its output reservation to the Foundry adapter, which
caps its configured completion limit accordingly. Tools and instructions are checked again
after compaction before coordinator inference.

The Foundry counter uses the model's BPE tokenizer over the serialized text-only request,
plus conservative framing headroom. It is a local admission estimate, not exact provider
billing usage. Configure headroom below the deployed model's actual context limit. See
[the tokenizer documentation][tokenizer] and [input-token counting guidance][counting].
No bytes-to-tokens ratio is used as a model-budget guarantee.

There is no new Redis summary or model-context cache. Assembly and retrieval use the primary;
existing Redis UI views and progress are independent and cannot become model context.
Consequently Redis loss requires no summary-cache invalidation. Any future model-context cache
must include tenant/owner/run scope, summary version, coverage/durable cursors, policy revision,
recovery/deletion generations and the complete prompt manifest. Passive reads retain the
existing inactivity behavior.

## Failure and bounded work

One read covers at most 16 turns, with 200 events and 256 KiB per turn. One inference handler
performs at most eight summary chunks by default (configurable to 32). These storage bounds
remain hard limits. Instructions, tool schemas and protected facts must independently fit.
For an exceptionally long turn, reduce the historical tail first, then summarize a completed
prefix. Cuts occur after complete exchanges; the latest exchange and unresolved calls remain
in the suffix. Publication stores `partial_turn`, its covered source cursor and `retained_cursor`
atomically.
The retained original question is excluded from prefix summaries and appears exactly once in
context.
Restart continues from that cursor. Pending approval and execution records remain authoritative.
An indivisible oversized exchange fails visibly without losing source history or replaying tools.

A proactive failure may return an independently checked fitting request. Mandatory failure
prevents inference and exposes a fixed reason. The existing optional-compaction argument remains
an internal test/application hook; normal queued startup needs no manual trigger or scheduler.
Successful chunks survive failure. Summaries must fit independently before publication; the
final assembled request, including the active suffix, must fit before coordinator dispatch.

Only a provider HTTP 400 with `context_length_exceeded` selects overflow recovery. PostgreSQL
records the rejected step and request fingerprint. Recovery must produce a smaller, locally
fitting request and atomically claim its single retry before dispatch. A crash after claiming
conservatively spends that allowance. Duplicate delivery cannot issue a third request. General
errors, timeouts and output-length exhaustion do not receive this recovery. Settled tools stay
in the journal and are never re-executed by compaction.

The former 65-second process-local pacing has been removed from the application and test relay.
The adapter makes one physical request. A real HTTP 429 supplies a sanitized bounded retry delay;
the worker persists that deadline and releases its slot. Existing attempt/run budgets still
bound retries. Summary calls share these limits. Provider telemetry reports numeric timing,
actual token usage and token-limit headers separately from estimated context measurements.
The provider can still throttle when offered traffic exceeds effective capacity.

A hosted release applies migrations 014–015 and runtime grants before updating application images.
See the [release guide](../../../infra/azure/LIVE.md). Production load, summary relevance across
banking workloads and automatic retention/deletion require separate evidence. Compaction grants
no journal-deletion permission. Redis UI views remain independent and disposable.

## Validation

See [group 11](../../../build-order/11-completed-semantic-compaction-and-retrieval.md)
for executed commands and acceptance evidence. Tests use synthetic conversations, real local
PostgreSQL/Redis, independent worker processes and a local Foundry HTTP transport. No live
model, Azure resource or existing conversation is needed by this test profile.

[tokenizer]: https://github.com/openai/tiktoken
[counting]: https://developers.openai.com/api/docs/guides/token-counting

Structured output uses the Foundry adapter with the documented Chat Completions format:
[OpenAI Structured Outputs][structured]. Shape enforcement complements source validation;
candidate identifiers still have to resolve to permitted durable sources. Every published
quotation is copied from its trusted candidate and validated against its original source.

[structured]: https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat

Explicit user name/ownership statements have a separate `identity` category. Conservative
English cues (`owns`, `owner`, `named`, `nicknamed`, `nickname`, `called`) preserve those exact
source excerpts even when the selector omits them. Assistant paraphrases receive no automatic
identity protection. Explicit source-validated corrections can retire an older identity; the
64-fact/32-KiB bounds still apply. This is bounded source retention, not general entity resolution.
Other details can still be omitted and require retrieval. Semantic retirement suggestions remain
conservative: a duplicate unresolved question may persist until a later valid selection retires it.

User excerpts containing explicit `confirmed`, `decided`, `correction` or `replaces` cues
likewise retain a decision label. For example, `Accessibility reviewer confirmed: Noor` survives
selector omission. These statements remain user assertions, including claims of completed work;
they never enter the completed-action section without a trusted completed tool source. The
static memory profile changes when these extraction semantics change, invalidating earlier
unsafe derived summaries while retaining every original journal record.
