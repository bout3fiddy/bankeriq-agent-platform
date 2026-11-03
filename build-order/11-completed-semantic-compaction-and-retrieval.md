# Semantic compaction and history retrieval

Status: completed. Normal queued startup includes automatic semantic memory and scoped history
retrieval. Requirements 1–13 and their acceptance gates passed with real local services and the
live hosted model/runtime. The Azure deployment remains running with its original conversation
preserved. Final performance evidence and remaining model/release limitations are recorded below.

1. **Persist useful conversation memory** — Require semantic memory in normal queued startup;
   register history retrieval without a per-chat opt-in. Store versioned summaries in PostgreSQL
   covering important decisions, confirmed action outcomes and unresolved work. Record
   source event references, covered cursor, summary format/prompt versions and generation
   fences. Preserve distinctions between proposed, approved, attempted, completed and
   failed actions. Summaries cannot authorise work or replace execution/approval records.
2. **Compact at a fixed boundary** — Summarise only permitted history through a fixed
   committed cursor. Publish the summary and its coverage atomically; newer events remain
   outside that coverage. Use stable operation IDs for retries and conditional revision
   checks to prevent stale summaries replacing newer ones. Retain source events under
   the existing recovery, audit and replay policies; compaction alone permits no deletion.
   Budget the summarizer's complete request against its selected model, including
   instructions, source text and reserved output space. Use bounded source chunks
   with explicit coverage when one request cannot fit; preserve tool-call/result
   pairs. Exclude stored provider reasoning and restricted content from summary
   input. Source byte limits alone do not establish a model-token budget.
3. **Assemble initial context** — Have the context builder load the latest compatible
   summary plus recent complete turns, alongside separately resolved system/runtime
   instructions and current authorised tools. Redis may cache this assembled history
   view; rebuild it from PostgreSQL after loss. Include summary version, covered/durable
   cursors, recovery/deletion generations and prompt manifest in relevant cache keys.
   Exclude later queued inputs from the active run and retain complete tool-call/result
   pairs. Keep bounded input budgets and explicit overflow handling. Distinguish
   optional background compaction from compaction required to fit the next request.
   If required compaction fails or its result still exceeds the budget, pause or
   fail with a bounded visible reason and a bounded retry policy. Keep the previous
   usable summary and source history; never dispatch an oversized request after
   that failure. Recheck the assembled context with its separately resolved prompt
   and tool definitions before model dispatch.
4. **Retrieve detail on demand** — Register a bounded, read-only history retrieval tool
   through the existing registry/gateway. Let the model request source events or earlier
   pages when the initial summary lacks detail. Trusted code supplies tenant, user,
   conversation and active-run visibility scope; model arguments cannot expand it.
   Recheck current access/output policy and return permitted excerpts with source cursors
   and continuation information. Redis can serve valid cached material; PostgreSQL
   supplies misses. Limit calls, items and bytes against the run/context budgets.
5. **Validate summary quality and isolation** — Treat summaries and retrieved content
   as conversation data. Prevent them from overriding trusted instructions or claiming
   unsupported action completion. Test provenance, omitted decisions/open questions,
   conflicting updates, revoked access, queued-turn isolation and confidential markers.
   Keep confidential payloads and delivery identifiers outside model-visible memory.
6. **Validate recovery and usefulness** — Exercise restart, Redis loss, concurrent
   compaction/new input, duplicate work, stale publication, deletion and database recovery
   generations. Check summary-plus-tail coverage without omission or duplication, and
   retrieve referenced source details through the tool. Compare long-conversation tasks
   against retained full-history fixtures to measure factual fidelity and retrieval needs.
   Test near-limit source history, oversized instructions/tool definitions, stored
   reasoning, summarizer failure and an oversized summary. A failed mandatory
   compaction must preserve prior memory, stop model dispatch and expose its reason;
   retries stay bounded instead of repeating an oversized request or empty answer.

Completion requires a long conversation to resume from durable semantic memory, recover
additional permitted detail on demand and preserve execution truth after cache/process loss.
Summary generation, retrieval and context assembly must remain independently testable.

## Automatic compaction improvements

**Items 7–13 are completed; expanded acceptance evidence follows the historical results.**

Added on 3 November 2026 following review of [Pi's compaction design][pi-compaction].
These are required follow-up items within group 11, with implementation and validation tracked here.
Historical evidence below retains its original scope; expanded evidence is recorded separately.
The final acceptance evidence covers these requirements together with the original gates.
Automatic compaction must be enabled in normal queued startup without a per-chat opt-in.
Do not add a manual compact command or require user intervention to trigger compaction.

7. **Preserve recent turns verbatim** — Add a configurable `keep_recent_turns`, initially two.
   Assemble trusted instructions, structured historical memory, the recent complete turns
   and the active turn in that order. A complete turn covers an admitted user input through
   its terminal response, including intervening tool exchanges. Preserve permitted message
   text, roles and tool-call/result structure in the retained tail instead of converting it
   to excerpts. Current access/output policy still applies; verbatim retention cannot expose
   restricted content, provider reasoning or later queued inputs. N is a retention target
   constrained by the full request budget. Coverage must prevent duplication and omission.
8. **Trigger proactively with headroom** — Check automatically before each coordinator model
   request, including continuations after tool completion. Trigger below a configurable free
   context reserve and compact toward a larger free-space target to avoid immediate repeated
   compaction. Count trusted instructions, tools, history and reserved output separately from
   this trigger reserve. Pin effective settings to prepared work for reproducible recovery.
   A proactive failure may use an independently checked fitting request; mandatory failure
   must stop dispatch. Keep attempts and chunks bounded. No background scheduler is required.
9. **Structure useful memory** — Organize summaries into goals, constraints, current decisions,
   completed actions, unresolved questions and next steps, with durable source references.
   Preserve validated excerpts and authoritative action states. Evaluate an optional concise
   narrative overview for continuity; any generated overview remains untrusted derived data
   and cannot establish approvals or completion. Measure recall and unsupported claims against
   retained full-history fixtures before adopting it. Summary and tail remain separately testable.
10. **Retire superseded facts from active memory** — Track explicit decision replacements and
    resolved questions, retaining source links and chronology for retrieval. For example, a
    confirmed CHF 1,500 revision replaces EUR 1,200 as the current budget while the earlier
    decision stays retrievable. An explicit answer can resolve an open question. Do not infer
    resolution or completion from omission or unsupported prose. Preserve durable execution
    records and prior summary versions; compaction grants no journal-deletion permission.
11. **Compact exceptionally long turns safely** — When the target N recent turns cannot fit,
    retain fewer complete historical turns first. If the active turn alone exceeds the budget,
    summarize its earlier completed exchanges while retaining its latest messages and every
    retained tool-call/result pair. Introduce explicit within-turn coverage; unresolved tool
    calls and pending approvals must remain reconstructible from authoritative records.
    If no safe bounded representation fits, stop with a visible reason and preserve history.
12. **Recover once from provider context overflow** — On a classified context-overflow rejection,
    compact further and retry inference at most once for that prepared model step. Persist the
    recovery allowance across process loss and duplicate delivery. Retain the failed attempt
    for audit, exclude unusable output from the retry context and preserve settled tool results.
    Never replay completed effects or treat an unknown provider outcome as a clean rejection.
    Recheck permissions, fences and the complete request budget before dispatch. General errors
    and output-length exhaustion require their own handling and do not trigger this recovery.

13. **Keep interactive provider admission responsive** — Remove the artificial rolling-minute
    quota wait. Dispatch once and respond to actual provider throttling with a bounded durable
    retry deadline that releases the worker. Preserve attempt budgets and unknown-outcome rules.
    Observe actual provider duration/token usage and effective quota separately from conservative
    context estimates. Validate adequate deployment capacity through inference headers as well as
    infrastructure configuration; record propagation delays and genuine throttling separately.

Implement verbatim retention, proactive triggering and fact retirement first; then structured
memory, within-turn coverage and overflow recovery. Preserve existing publication transactions,
permission checks, execution-state separation, retrieval limits and restart fencing throughout.

### Additional acceptance gates

- Verify automatic default wiring, no manual trigger dependency, threshold boundaries and
  headroom behavior across repeated turns and tool continuations, including model/profile changes.
- Compare the retained tail with permitted original messages for N=0, N=2 and budget-reduced N;
  verify roles, text, tool pairs, source coverage and exclusion of later queued or revoked data.
- Exercise repeated corrections and explicit question resolution over a long conversation;
  check bounded active memory, correct current answers and retrieval of superseded sources.
- Exercise a single long active tool workflow, pending calls and approvals, process restart,
  cancellation and stale publication. Verify no missing messages or duplicated tool execution.
- Inject a provider overflow despite a passing local estimate; verify one durable retry, no
  second recovery after restart, preserved audit history and explicit failure if fitting fails.
- Extend live-model recall/retrieval evidence to the new summary and tail format. Record factual
  fidelity, unsupported action claims, compaction frequency and token estimates with their limits.

- Assess short-answer duration against PERF-003, with at least 100 comparable attempts for one
  and two active-conversation cohorts separately. Record every failure and other measured targets.
- Exercise at least 20 representative live summary generations: every successful compaction
  should take at most ten seconds, preserve checked facts and produce no unsupported action state.
  This collection is a correctness/speed gate and does not establish a compaction percentile SLO.
- Exercise actual and injected provider throttling, checking durable retry deadlines, released
  worker slots and bounded attempts. Routine successful calls must have zero artificial quota wait.

[pi-compaction]: https://pi.dev/docs/latest/compaction

## Implementation

The [memory guide](../src/bankeriq_agent_platform/memory/README.md) records composition,
transaction boundaries, explicit limits and required queued startup wiring.

- Migration 013 stores versioned PostgreSQL summaries, covered event/admission cursors,
  prompt/format provenance, generation fences, summary attempts and charged retrieval results.
  Journal, approvals, execution state, audit and delivery records remain authoritative.
- `SemanticMemory` builds fixed-scope summary-plus-tail requests. Whole-turn chunks have
  separate model-specific token budgets, including instructions and output reservations.
  Prepared coordinator jobs pin their memory profile; summary operations pin their prompt.
  Migrations 014–015 add partial-turn coverage, retained original input and durable one-time
  overflow recovery. Recent permitted turns retain original roles, text and tool pairs.
- A strict JSON schema lets the live model select precomputed source candidate IDs. Trusted
  code restores exact excerpts and source attribution. Trusted source kinds and compact canonical
  action states prevent generated text from fabricating an approved/completed state.
  Protected goals, constraints, decisions, questions and action states survive model omission.
  Explicit validated replacements retire prior decisions/questions with durable source links.
- `retrieve_history` is a scoped, read-only registered tool. Current gateway/resource policy,
  source policy, call/item/byte limits and durable duplicate accounting apply. Its child uses
  the existing asynchronous tool queue and one join continuation.
- Publication checks the current worker lease, run revision, summary version, attempt token,
  deletion generation and recovery UUID in its database transaction. Optional failure can
  return an already-fitting request; mandatory failure stops coordinator inference with a
  fixed visible reason. Both preserve existing source history and usable summary versions.
- No Redis model-context cache is introduced. Redis loss rebuilds UI state normally while
  model memory comes from PostgreSQL. Clients, credentials, networking and scheduling remain
  application/provider responsibilities. The synchronous profile remains unchanged.

## Reproducible local verification

Run from the repository root with uv and dedicated disposable services. These commands
create local Docker containers only; they do not contact or modify the retained Azure runtime.
Do not substitute a production database or cache: tests perform migrations and recovery fencing.

```sh
docker run -d --name bankeriq-memory-pg \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55449:5432 postgres:17
docker run -d --name bankeriq-memory-redis -p 127.0.0.1:56399:6379 redis:7
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56399/0'
uv run pytest tests/unit/conversations/test_semantic.py \
  tests/integration/test_semantic_memory.py -q
uv run python scripts/verify.py
git diff --check
```

Wait for PostgreSQL readiness before running the suite:
`docker exec bankeriq-memory-pg pg_isready -U postgres`.
Cleanup after validation uses `docker rm -f bankeriq-memory-pg bankeriq-memory-redis`.
These names belong only to this task's local validation services.

Executed on 3 November 2026 with Python 3.14, PostgreSQL 17.11, Redis 7.4.12 and
`tiktoken` 0.14.0. Initial synthetic verification passed 527 tests in 89.87 seconds.
Expanded default-wiring, candidate-selection, strict-output, permission, historical-retrieval
projection, transcript-owner and dispatch-pacing checks passed **536 tests in 100.58 seconds**,
with zero skips, using `uv run python scripts/verify.py`. A later rerun passed the same
536 tests in 105.87 seconds. Final verification after transcript tooling and documentation
passed **536 tests in 97.74 seconds**, zero skips; log: `/tmp/group11-final-verify.log`.
Lock, format, lint, readability, JSON, line-length and type checks passed.
`git diff --check` and local Markdown link checks also passed.
The release verification log is retained locally at `/tmp/group11-final-release-verify.log`.

## Acceptance evidence

1. The 24-turn retained-history fixture keeps three checked facts: the original EUR
   decision, its later CHF correction and the unresolved deadline question. Source order
   supports the corrected answer. Original details omitted by extraction are recovered
   through `retrieve_history`. This is a deterministic synthetic fidelity fixture.
2. Real PostgreSQL tests cover publication rollback, lost commit acknowledgement, duplicate
   publication, conflicting content, stale attempts and competing summary versions. A new
   input admitted during summary inference stays outside the fixed coverage and model request.
3. A worker process is killed after reserving summary dispatch. A fresh independent worker
   reconstructs the operation, consumes its remaining attempt and completes the run. The
   original prompt survives prompt activation changes; incompatible static profiles block.
4. Real Redis loss followed by a fresh worker process preserves semantic memory and final
   conversation output. Deletion, database recovery-generation change, cancellation and lease
   expiry during inference reject late summary publication. No cloud restart is required.
5. Retrieval runs through ordinary discovery, gateway execution, durable tool settlement and
   exactly one join continuation. Duplicate delivery reuses its charged result. Tests reject
   owner/tenant substitution, model-supplied scope, revoked access and a fifth retrieval call.
6. Restricted references, delivery identifiers, approval identifiers, provider reasoning and
   raw provider/policy error markers are absent from summary input, saved summaries, retrieval
   output and model context. Delivery metadata remains separately available to its owner.
   Recorded proposed, approved, failed and completed actions retain distinct source kinds.
7. Full-request token tests include non-ASCII text, special-token-like input, instructions,
   tool schemas and output reserves. Real SDK calls over a local HTTP transport cap summary
   output at its reserved 512 tokens and send no tools or provider storage request. Native JSON
   schemas are snapshotted, counted and sent unchanged; unknown candidate IDs and altered
   quotations fail validation. The initial hosted summary profile reserved 2,048 output tokens;
   the revised candidate selector reserves 1,024.
8. Near-limit source budgets stop before summarizer dispatch. Oversized instructions/tools,
   invalid/generated-overlarge summaries, oversized valid summaries, unavailable prompts,
   failed access checks and provider failure block coordinator inference with bounded reasons.
   Existing usable summaries survive failure. Optional failure preserves fitting context.
   Existing context byte/item limits, worker budgets and bounded summary retries remain enforced.

## Live-model evidence

The [complete local live-model transcript](evidence/11-local-live-model-transcript.md)
records 22 turns against the existing private Foundry gpt-4o-2024-11-20 deployment.
Each local turn used fresh coordinator/tool processes, local PostgreSQL and Redis, and
normal queued composition. It contains the actual replies and all three published summaries.
The [18-turn replay fixture](../tests/azure/semantic_dialogue.json) omits exploratory failed
attempts; the transcript preserves them. The [harness guide](../tests/azure/README.md) records
private inference transport, commands, budgets and scoped exports.

- Four exploratory turns failed: rapid inference traffic exhausted three bounded attempts;
  three later summary attempts rejected fenced, paraphrased or misattributed excerpts.
  Source journals survived. The stopped run was cancelled through the normal scoped control;
  no budget was reset. Native JSON plus candidate selection resolved the summary failures.
- Turn 20 published versions 1 and 2, covering admission turns 11 and 15. Its answer preserved
  CHF 1,500 replacing EUR 1,200, named responsibilities, unresolved reviewer/language questions,
  the prototype name and the completed arithmetic result. It used `retrieve_history` through
  the ordinary tool queue when the summary omitted detail. The local database contains three
  tool groups (calculation plus two retrievals), all joined once, and two retrieval receipts.
- After this conversation's Redis view was removed, turn 21 correctly recalled the budget,
  previous currency and prototype. Its fresh workers completed in 4.185 seconds.
- Turn 22 repeated the cache-loss check after the final history projection fix. It published
  version 3 under the final profile, retrieved source page `after=0, limit=1`, and returned
  the omitted exact label `LANTERN-7B` plus the corrected CHF 1,500 budget. It completed in
  134.425 seconds including two quota waits. Prior nested retrieval pages now contribute a
  receipt; original user claims cannot become historical tool-result facts.

The [hosted transcript](evidence/11-hosted-live-model-transcript.md) records 18 completed turns
on the upgraded Azure runtime. Turn 17 automatically published one nine-entry summary covering
11 turns; its recall preserved the corrected budget, prototype and unanswered questions.
Turn 18 recovered `LANTERN-7B`, which was absent from that summary, through a scoped history page.
Two history calls shared one tool group and one durable continuation. All observed model/tool
jobs and the summary operation completed on their first attempt. No stopped job remained.

After turn 18, deleting only its Redis history view reconstructed cursor 62 and preserved
all 42 journal events and the summary. The original conversation retained five runs, 14 events,
18 completed jobs and cursor 21. Terraform changed only the migration and three app images;
its final refreshed plan reported no changes. The hosted transcript records the immutable image,
release timings, bounded quota waits and an unnecessary second history read.

The summary also labelled an answered tools-description request as an open question and omitted
some useful details supplied by recent complete turns. This measured selection-quality limit
is retained in the transcript. Source fidelity and execution isolation passed; semantic labels
and completeness require broader workload evaluation.

## Remaining limits

The summary format is extractive. A generated narrative overview was considered but has not
been adopted or empirically compared; it would introduce rewritten facts beyond the validated
source-selection format. No narrative-quality advantage is claimed.
Deterministic fixtures and the fictional live conversation
establish source fidelity and recall for those cases; banking workload quality and broader
model evaluation remain separate work. Protected facts can eventually exceed a configured
budget, producing an explicit failure while preserving the journal and earlier summaries.

Token accounting uses the selected model's tokenizer with framing headroom and a configured
safe window. It is an admission estimate, not provider billing usage. Candidate extraction
splits source text at sentence/line boundaries. Coverage normally advances by complete turns;
exceptionally large turns use a completed prefix with explicit retained original-input coverage.
An indivisible oversized latest exchange fails before dispatch. Tool pairs remain intact.
Background scheduling, Redis model-context caching and automatic retention/deletion remain
unimplemented. Compaction permits no journal deletion; group 13 owns storage lifecycle.

The previous process-local 65-second pacer is removed. Genuine provider throttling uses durable
retry deadlines and existing attempt budgets. Available provider capacity remains a throughput
boundary; fast isolated requests do not establish overload behavior or a billing guarantee.
Historical transcripts include the previous pacer and remain evidence of that earlier release.


## Expanded validation and exploratory findings

The first implementation of items 7–13 passed 566 real-service tests, with no skips, in
112.18 seconds (`/tmp/compaction-full-verify-2.log`). Live follow-up then exposed two quality
issues: a semantically unsupported retirement suggestion blocked otherwise valid summaries,
and within-turn compaction could remove the active question after history retrieval. Both
have subsequent code fixes and focused regression tests; the earlier full pass is not final
release evidence for those fixes. The complete synthetic dialogue ledger retains failed turns.

Pi's [compaction implementation][pi-code] informed automatic threshold checks, retained suffixes
and tool-pair cut boundaries. The inspected file blob was
`07a58ac6cf6e2141fc07133677583ab18bb97298`. This runtime adds durable worker fencing, explicit
retained user-input coverage, source-validated retirement and a persisted single overflow retry.
No Pi dependency or source code was copied into the runtime.

The existing Azure model deployment capacity was changed in place through a reviewed Terraform
plan, from 10 to 50 quota units. Azure management confirmed 50,000 TPM before inference headers
updated; a fresh probe after propagation confirmed 50,000 TPM and 300 RPM. The initial dialogue
records real 429 waits during that interval. These samples are not the final latency cohort.

[pi-code]: https://github.com/earendil-works/pi/tree/main/packages/coding-agent/src/core/compaction

The newer trials additionally exposed omitted user identity statements and a lost explicit
reviewer confirmation during a full profile rebuild. Conservative extraction now preserves
explicit user name/ownership and confirmation/correction excerpts independently of model
selection. Their source kinds remain user assertions, never approvals or completed tool actions.
General semantic relevance remains model-dependent; original sources remain retrievable.
The initial hosted trial is retained in
[its transcript](evidence/11-hosted-automatic-compaction-initial.md), including wrong answers.
The subsequent hosted trial correctly recalled the checked current facts and recovered the
superseded budget through scoped retrieval. Final evidence follows.


## Final expanded acceptance evidence

- `uv run python scripts/verify.py` passed **574 tests in 136.17 seconds, zero skips**, with
  PostgreSQL 17.11, Redis 7.4.12 and independent worker processes. Log retained locally at
  `/tmp/compaction-assertions-verify.log`. Static formatting, lint, readability, JSON, line-length
  and type checks passed. The final documentation/link checks and `git diff --check` passed.
- `test_compaction_improvements.py` covers N=0/N=2 and budget-reduced tails, threshold equality,
  headroom hysteresis, proactive fitting fallback, completed active-turn prefixes, original-input
  retention, independent-process restart, pending approval/job preservation and cancellation.
  Clean overflow has one durable retry; a lost worker cannot obtain a second allowance.
  Real SDK local transports classify overflow/rate errors without hidden SDK retries or
  payload logs.
- Source validation preserves exact quotations, kind and cursor. Unit tests exercise 100 explicit
  corrections, resolved questions, unsupported retirement advice, repeated selector omission,
  identity correction and confirmation preservation without promoting user claims to tool outcomes.
  Existing permission, output isolation, duplicate settlement and unknown-effect tests all pass.
- The final live summary cohort has **20/20 successes**, 0.690–2.558 seconds, all checked facts
  preserved and no unsupported completed-action state. Duplicate questions can persist until later
  validated retirement advice; the final long-chat summary has resolved its reviewer/language
  questions. General semantic quality remains a stated limitation.
- The [27-turn hosted transcript](evidence/11-hosted-automatic-compaction-transcript.md) includes
  all 11 summaries, explicit budget corrections, resolved questions and source retrieval. The final
  image rebuilt compatible memory, then Redis-view loss recovered without regenerating summaries.
  Current-fact recall took 2.144 seconds; source-detail retrieval took 3.907 seconds through
  one tool child and one joined continuation. The transcript retains the earlier unclear historical
  answer and the later explicit retrieval; model tool choice is not guaranteed on every question.
- Two separate **100-attempt** hosted short-answer cohorts passed PERF-003: median/p95
  1.237/1.623 seconds serial and 1.264/1.651 seconds paired. Every response was correct, with
  one model call, 1,121 actual input tokens, two output tokens and no throttling. PERF-001 also
  passed. PERF-002 overhead remains above target; PERF-004/005 remain unmeasured. These assessed
  targets and the offered load are documented in the [performance report][final-performance].
- The initial 10,000-TPM propagation trial retained 19 real rate-limit responses and bounded
  retries. After effective capacity reached 50,000 TPM, normal dispatch had no artificial wait.
  The final image is pinned in `infra/azure/LIVE.md`; migrations through 015 succeeded. All old
  worker revisions stopped, all current roles reached `--0000004`, and the final Terraform plan
  reported no changes. One mixed-profile rollout request failed safely; this release limitation
  and cold profile-rebuild durations are retained in the performance report.

The original user conversation remains five runs, 14 journal events, 18 completed jobs and
cursor 21. No cloud resources or original data were removed. The synchronous in-memory profile
remains unchanged. Raw transcripts and snapshots remain local in the gitignored evidence folder.

[final-performance]: ../performance/records/2026-11-03-responsive-compaction.md
