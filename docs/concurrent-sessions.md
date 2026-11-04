# Concurrent sessions and streaming

Group 12 is in progress. The implementation supports several conversations owned by
the same authenticated user. Each conversation has its own admission order, active run,
history, semantic memory and Redis keys. One busy conversation can queue its next turn
without taking ownership of another conversation. Workforce browser sign-in remains group 13.

## Execution and delivery

The pilot configuration selects eight execution slots globally and five per tenant for each
queue. Current source selects five preparation and five inference tasks per coordinator;
tools use two tasks. Hosted revision 12 uses these settings. A separate two-call semaphore bounds
summary generation per coordinator.
These are configured limits; hosted throughput and latency qualification remain pending.

Workers use PostgreSQL notifications to shorten idle claim waits. A lost notification still
recovers through the existing 250-ms bounded scan. Listeners belong to worker schedulers.
An idle conversation has no worker task or dedicated thread. Active SQL/cache operations use
the bounded process pool; network and scheduling remain outside the harness.
The scheduler also wakes when an execution finishes, allowing a freed slot to claim work
while its one existing notification wait remains active. Shutdown drains that wait before
closing the listener; completing many short jobs cannot accumulate socket-reader threads.

Admission accepts an optional `worker_budget` from one through 100, defaulting to 100.
Clients can reduce the existing allowance for a bounded experiment; they cannot raise it.
For example, eight attempts cover preparation, model/tool claims and charged summary/recovery
dispatches together. The budget is pinned with admission, inherited by children and unchanged
by retries. Repeating a request with a different allowance returns a conflict. Exhaustion
keeps the existing explicit outcome and unknown-effect handling.

The async Foundry adapter collects and validates complete responses while optionally forwarding
text deltas. Tool argument fragments remain private until the complete call validates. Structured
output requests remain buffered. Application startup supplies an explicit publication policy;
the pilot enables provisional text only for its allowed non-sensitive operator conversations.
Applications requiring whole-answer review must leave provisional publication disabled.
Azure's [prompt-filter metadata frames][azure-stream] contain no answer and may use an empty
`object` field. The adapter normalizes only that exact metadata envelope before strict chunk
validation. Metadata still consumes the event/byte budget; it cannot complete an answer.

[azure-stream]: https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/content-streaming

Each run keeps one cumulative Redis text snapshot, bounded to 32 KiB, with model step, job,
ownership token and sequence. A slow Redis writer coalesces pending snapshots. Primary checks
before publication and delivery reject expired owners and cancelled runs. Provider failures
reset the draft; normal durable settlement supplies the final answer. Report delivery metadata
remains separate from model-visible text.

The SSE protocol exposes:

- `text`: provisional cumulative text with run, step, attempt and sequence.
- `reset`: discard provisional text; progress may have expired or changed ownership.
- `final`: durable terminal state and permitted answer, replacing the draft once per client run.
- `saved`: bounded journal progress with a durable reconnect cursor.
- `view`: scoped run/child status and separate delivery metadata.
- `reconnect`: close and resubscribe, retaining the delivered durable cursor.

One connection buffers at most 32 events and 256 KiB. Overflow sends `slow_consumer` and closes
that reader. An API process accepts at most 64 streams and eight per user. These limits aggregate
across the configured replicas; they are not cluster-global reservations. Five-minute reconnects
reauthenticate. Fixed keepalive comments every fifteen seconds keep idle transport alive.
Passive reads, comments and open streams never renew conversation activity.

Final delivery queries the primary directly, independently of Redis or older history pages.
Clients may reconnect to a different replica with the same run and cursor. A disconnected or
timed-out client can resume that run; it must not manufacture a new admission to recover it.
`POST /runs/cancel` explicitly cancels an owned run. Dispatched effects retain their existing
unknown-effect records and recovery restrictions.

## Reference client

Use the existing operator configuration and Azure CLI delegated login. No browser sign-in or
new employee account is needed for several isolated conversations under that login.

```sh
uv run python -m agent_platform.client --config scratch/test-client.json new
uv run python -m agent_platform.client --config scratch/test-client.json send \
  --text 'Explain what a bank statement contains.'
uv run python -m agent_platform.client --config scratch/test-client.json resume
```

Copy the existing non-secret configuration to a distinct file first. Each concurrent client
needs its own file. The client stores run IDs before POST, keeps credentials in memory, decodes
bounded fragmented UTF-8/SSE, rejects stale attempts and sanitizes terminal control characters.
Its default wait is 30 seconds. Resuming performs reads and never resubmits the message.
Install the repository's locked development dependencies; `httpx2` is used for this reference CLI.

## Release procedure

Migration 017 adds a primary admission gate and audited compare-and-swap release decisions.
Bounded pilot admission allows at most 64 unfinished runs globally, 16 per tenant and two per
conversation. Repeating an already accepted request remains valid while admission is closed.
Unaccepted overload returns HTTP 429; a release pause returns HTTP 503 with `Retry-After`.

The operator-only `GET /operator/release` reports the gate revision and unfinished work.
`POST /operator/release` accepts `request_id`, `open` and `expected_revision`. Application policy
must explicitly authorize this global control; ordinary conversation ownership provides no grant.
Repeated control IDs resolve their recorded result, including an unknown commit acknowledgement.

For compatible releases, new workers match saved recovery arguments before claiming work.
The prepared memory profile is part of that selection, including queue-pressure observations.
Incompatible work stays durable for a compatible consumer without charging an attempt.

For an incompatible release or rollback:

1. Close admission and stop benchmark senders. Keep the exact old image available.
2. Drain accepted model, tool and compaction work on compatible consumers. Inspect stopped,
   approval and unknown-effect work through the existing authorized recovery controls.
3. Verify no unfinished accepted work remains. Do not force completion or reset budgets.
4. Set `runtime_paused=true` in the reviewed Terraform plan. This retains the applications,
   disables ingress and deactivates the paused revisions through Terraform's AzAPI provider.
   Verify every old replica has terminated before continuing. A zero minimum alone left all
   three consumers alive during the first hosted rehearsal.
5. Run the new migration job, then activate the new image with `runtime_paused=false` through
   Terraform. Changed prompt
   activation refuses an unfinished-run backlog. All old pinned prompt assets remain available.
6. Verify consumers and readiness while admission stays closed. Reopen for one bounded
   synthetic canary before starting other traffic; close admission again if the canary fails.

The previous group-11 image predates this gate/profile routing. Its first upgrade required a
maintenance window with accepted work checked before prompt activation. A newly ready API
cannot prove that old queue consumers have exited.
Rollback drains new-profile work on its compatible image before switching consumers back.
Database tables and retained conversations survive; migrations are additive.

The pause plan updates applications and records one [deactivation action][deactivate] per role.
No application or data service is deleted. Removing an apply-time AzAPI action on resume has
no remote effect; the new revision restores capacity. Verify actual termination before migration.
The revision target is read after app updates using an [AzAPI data source][revision-read] with
an explicit [dependency][revision-dependency]. A pause can itself create a new revision;
the earlier direct AzureRM attribute fixed the old name during planning and failed during apply.
The new target remains unknown until that dependent read completes. The regression plan and
fourteen Terraform tests passed. The revision-12 live pause deactivated all three correct targets.

[deactivate]:
https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps-revisions/deactivate-revision?view=rest-resource-manager-containerapps-2025-07-01

[revision-read]:
https://registry.terraform.io/providers/Azure/azapi/latest/docs/data-sources/resource
[revision-dependency]:
https://developer.hashicorp.com/terraform/language/meta-arguments/depends_on

## Measurements and remaining gates

Migration 016 records admission and terminal-write timestamps. Migration 019 uses native xid8
conversion across transaction-ID epochs while preserving migration 016's applied bytes. Exact
commit timestamps require PostgreSQL `track_commit_timestamp`; an unavailable timestamp stays
null. Terminal-write time supplies a conservative delivery-delay upper bound. The measurement
client retains a primary-clock offset interval instead of assuming synchronized clocks.

Lifecycle events correlate run, job and attempt without application payloads. Admission,
assembly, claim queue age, provider, settlement and provisional-publication timings have distinct
boundaries. Migration 018 adds scoped numeric usage observations through a bounded asynchronous
writer. Failed/overflowed observations emit `telemetry.dropped`; missing usage remains unknown.
A killed process can lose its last observation. This ledger cannot establish complete billing
coverage, and its retention belongs to the storage lifecycle work item.

Claim telemetry distinguishes candidate-selection age from a conservative due-to-commit
acknowledgement upper bound. The latter includes the remaining claim transaction and may also
include pre-eligibility capacity/resource/ordering waits. Unknown-acknowledgement recovery has
no timing sample. Revision 7 emits only candidate age and cannot establish the complete queue
wait gate; deployed revision 8 adds `queue_wait_upper_ms` without extra SQL.

Local source additionally supplies one passive process monitor per role for current Linux RSS,
CPU, tasks, threads and pool counters. Coordinator probes include active stopped work, using
the additive migration-020 attention index, so old unknown effects cannot disappear merely
because their error logs leave the query window. It does not dispatch work or renew activity.
These additions and migration 020 are deployed in revision 8 after the revision-7 soak.
The four approved Azure Monitor rules and notification limits are documented in the
[operator runbook](../performance/queries/README.md).

The [first hosted baseline](../performance/records/2026-11-04-hosted-streaming-baseline.md)
completed all 95 accepted measured turns, with five scheduled arrivals held as client backlog.
Completion p95 was 2.86 seconds. First text and application overhead missed their targets;
this does not establish the proposed supported envelope.

Memory selection now recognizes declared Dutch and mixed-language identity/correction cues.
System prompt 1.1.0 introduced bounded automatic retrieval guidance for omitted source details.
Hosted system 1.2.0 adds current response-format guidance and memory profile v10 permits empty
selections while protecting decisions and tool states. A bounded coverage marker keeps omitted
history discoverable after an empty selection. Revision 10 includes these fixes. Earlier
prompt files remain available for pinned recovery. The [full live memory suite][memory-coverage]
passed twenty scenarios twice and all 74 expected values. Sixty directly measured remote
summary calls took 0.52–2.53 seconds. Two incidental-description relevance exceptions remain.
Earlier [baseline](../performance/records/2026-11-04-memory-baseline.md) and
[candidate](../performance/records/2026-11-04-memory-candidate.md) failures remain recorded.
The [hosted cohorts](../performance/records/2026-11-04-hosted-concurrency.md) completed 100/100
correct turns at two/five sessions, with first-text/overhead targets still missed. Controlled
alert firing and recovery passed for all four rules.
The [two-hour soak](../performance/records/2026-11-04-hosted-soak.md) passed its functional
checks; process RSS/tasks/pool-wait evidence was unavailable in that earlier image.
The user approved the staged hosted upgrade on 4 November. Migrations through 019 ran after
old consumers stopped, and the original 280 terminal runs were preserved. The first image's
streaming canary exposed the Azure metadata envelope above; the failed canary and bounded
attempts remain recorded. Consult [LIVE.md](../infra/azure/LIVE.md) for the current image.

Run local independent-process cohorts against explicitly disposable PostgreSQL/Redis:

```sh
uv run python -m tests.scaling.conversations \
  --output scratch/conversations-two-workers --workers 2 --slots 2 --count 100
uv run python -m tests.scaling.conversations \
  --output scratch/conversations-one-worker --workers 1 --slots 4 --count 100
uv run python -m tests.scaling.conversations \
  --output scratch/conversations-increased --workers 2 --slots 4 --capacity 8 --count 100
```

These local synthetic responses exercise execution/delivery, with real HTTP and services.
They establish no live-model latency or memory-quality claim. Each run creates and removes only
its own schema/processes, preserving safe logs and every attempted outcome. The workload records
scheduled arrivals, client backlog, admitted timeouts and separate durable reconnect recovery.
The [soak runner](../tests/soak/README.md) uses the existing operator and two isolated
conversations.
Hosted execution requires a reviewed image/plan and explicit release/load approval.

The [mixed workload](../tests/mixed/README.md) prepares paired ordinary/compaction arrivals
with conservative attempt reservations. Its [hosted record][mixed-result] contains 101 comparable
one-compaction turns and 103 ordinary turns with actual summary overlap. Local generation/head
read batching preserves recovery locks and passed a balanced
[network-delay experiment](../performance/records/2026-11-04-postgres-read-batching.md).
Revision 12 adds ownership read batching, completion wakeup and active-memory source reuse.
Its full local gate passed 717 tests with zero skips; hosted latency qualification is underway.

[memory-coverage]: ../performance/records/2026-11-04-memory-coverage.md

[mixed-result]: ../performance/records/2026-11-04-hosted-mixed.md

The [current qualification checkpoint][qualification] records all 300 realistic supported-tier
turns, the 100-turn overload with twenty recovered stream rejections, and the separate control.
All accepted turns completed correctly; remaining latency targets keep group 12 in-progress.
Revision 12 is live and its original user conversation remains intact. The campaign is reconciled
at 2,243 calls, leaving 257; a new three-tier qualification needs a larger bounded allocation.

[qualification]: ../performance/records/2026-11-04-group12-qualification.md
