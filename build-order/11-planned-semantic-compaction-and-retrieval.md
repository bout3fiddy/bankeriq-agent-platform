# Semantic compaction and history retrieval

Status: planned follow-up after the infrastructure groups. Depends on Redis sessions,
durable workers and asynchronous coordinator/tool queues. Existing execution checkpoints
persist run state, budgets and outcomes; semantic action/decision summaries are not implemented.

1. **Persist useful conversation memory** — Store versioned summaries in PostgreSQL
   covering important decisions, confirmed action outcomes and unresolved work. Record
   source event references, covered cursor, summary format/prompt versions and generation
   fences. Preserve distinctions between proposed, approved, attempted, completed and
   failed actions. Summaries cannot authorise work or replace execution/approval records.
2. **Compact at a fixed boundary** — Summarise only permitted history through a fixed
   committed cursor. Publish the summary and its coverage atomically; newer events remain
   outside that coverage. Use stable operation IDs for retries and conditional revision
   checks to prevent stale summaries replacing newer ones. Retain source events under
   the existing recovery, audit and replay policies; compaction alone permits no deletion.
3. **Assemble initial context** — Have the context builder load the latest compatible
   summary plus recent complete turns, alongside separately resolved system/runtime
   instructions and current authorised tools. Redis may cache this assembled history
   view; rebuild it from PostgreSQL after loss. Include summary version, covered/durable
   cursors, recovery/deletion generations and prompt manifest in relevant cache keys.
   Exclude later queued inputs from the active run and retain complete tool-call/result
   pairs. Keep bounded input budgets and explicit overflow handling.
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

Completion requires a long conversation to resume from durable semantic memory, recover
additional permitted detail on demand and preserve execution truth after cache/process loss.
Summary generation, retrieval and context assembly must remain independently testable.
