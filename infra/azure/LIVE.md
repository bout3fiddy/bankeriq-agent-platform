# Persistent operator test deployment

Source refactor note (4 November 2026): the repository now uses `agent_harness`,
`agent_runtime` and `agent_platform`. This structural change has not been deployed.
The image/digests and historical evidence below retain their original meaning. HTTP chat
commands remain compatible; container-side Python diagnostics must use the source version
matching the deployed image until an explicitly authorised application upgrade occurs.

This environment is authorised to remain running for non-sensitive conversations and
idle-cache experiments. It incurs ongoing Azure charges until explicitly stopped and
removed. It is a single-operator test deployment, using the existing group-09 harness.
The resource group is `bankeriq-live-runtime` in Sweden Central; resource names begin
with `bankeriq-live-415eb635`. Container Apps and the migration job use the shortened
prefix `bankeriq-li-c19afe58` to fit Azure name limits. Local Terraform state and
`runtime-live.tfvars` own this environment.

## Runtime and retention

- One API, one coordinator and one tool Container App, each with 0.5 CPU and 1 GiB.
  Fixed minimum/maximum replicas keep the processes available while conversations idle.
- Private PostgreSQL 17, General Purpose D2ds v5, 32 GiB, seven-day backups, single primary.
- Private Azure Managed Redis Balanced B0, finite 30-minute session idle TTL and LRU eviction.
- Foundry `gpt-4o` version `2024-11-20`, regional Standard, capacity 50 (50,000 TPM).
- Separate managed identities, Entra operator allowlist, immutable registry image and
  private network/DNS. Only the coordinator can call inference.
- Existing Log Analytics collects application JSON lifecycle events and platform logs.
  Retention is 30 days with a configured 1 GiB daily ingestion cap.

Accepted messages, explicit `POST /activity` signals and unexpired durable worker leases
renew Redis retention. An open SSE stream, status polling, history reads, inspection and
background projection do not count as user activity. Repeated user signals coalesce
within 15 seconds. Every key retains a finite expiry; no thread is reserved for an idle chat.

PostgreSQL commits accepted messages and execution transitions directly. Redis expiry
does not trigger a PostgreSQL write or delete the conversation. The next turn uses durable
history. A passive read after expiry reconstructs the response without recaching an idle
view; a new interaction permits caching again. Redis eviction/outage is still possible
during activity. Durable state is the recovery source in those cases too.

## Send messages and inspect

Run from the repository root with the authorised Azure CLI login. The local ignored
`scratch/live-client.json` stores the URL, audience and current conversation/run IDs;
it contains no token. Each invocation obtains a delegated token in memory.

```sh
uv run python -m tests.azure.chat send --text 'Remember the synthetic codeword amber-lantern.'
uv run python -m tests.azure.chat inspect
uv run python -m tests.azure.chat read
```

`send` waits for the current turn and prints its response, timings and separate delivery
metadata. `inspect` reports Redis TTLs, PostgreSQL event/run counts and job states. Redis
TTL `-2` means the key is absent. Both `read` and `inspect` are passive. To deliberately
signal interaction without submitting a message, use the `activity` action. A future UI
can call that endpoint on explicit user interaction; periodic idle heartbeats defeat expiry.

For the hour-or-two experiment, preserve the current conversation. After the idle period,
run `inspect` first, then send a question about the earlier codeword and inspect again.
This exposes cache absence before the new message and restored context afterward.
Use `new` only when intentionally starting a separate conversation. This opt-in deployment
accepts non-sensitive messages up to 8,192 UTF-8 bytes; it is not a sensitive-input classifier.

## Backend logs

Lifecycle events include acceptance, cache hits/misses/reconstruction, explicit activity,
worker claim/start/settlement/retry/uncertain outcome and startup/drain. Fields are time,
hashed tenant, opaque conversation/run IDs, allowlisted queue, attempt and elapsed time.
Message text, prompts, tool arguments/results, credentials, error payloads and report URLs
are excluded from the application event interface. PostgreSQL remains the durable audit
and execution record. Operational logs have their separate retention limit.

```sh
live_logs=$(terraform -chdir=infra/azure output -json hosted_foundation | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["log_workspace"])')
az monitor log-analytics query --workspace "$live_logs" --analytics-query \
  'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(1h) | extend e=parse_json(Log_s)
  | extend app=tostring(column_ifexists("ContainerAppName_s", ""))
  | where isnotempty(e.event)
  | project TimeGenerated, app, e | order by TimeGenerated asc' -o json
```

Filter `tostring(e.conversation)` to the ID in the client file to follow one conversation.
Ingestion can lag; immediate console logs are also available through `az containerapp logs show`.
Microsoft documents a [several-minute ingestion delay][log-delay] for saved Container Apps logs.
The optional app-name field lets the query work before the first app records arrive;
the migration job creates this table initially with job-specific columns.
No additional logging service is needed for this experiment. Alerting, production telemetry
budgets and database lifecycle/capacity controls remain separate work.

## Reproduce the release

The ignored variables select `runtime_enabled=true`, `runtime_interactive=true`,
`runtime_session_idle_seconds=1800`, `runtime_replicas=1` and `runtime_ha=false`.
All cloud resource creation and changes use Terraform. CLI operations publish artifacts,
invoke the existing migration job, inspect Azure and call the runtime.

```sh
terraform -chdir=infra/azure plan -var-file=runtime-live.tfvars -out=live.tfplan
terraform -chdir=infra/azure show live.tfplan
terraform -chdir=infra/azure apply live.tfplan
```

The executed release sequence is foundation, linux/amd64 immutable image, migration job,
successful migration, then `runtime_ready=true` and the three apps. Each step uses a
separate saved and reviewed plan. The [harness guide](../../tests/azure/README.md) records
image publishing and migration commands. Do not substitute the old disposed pilot variables.
Terraform state is local and protected for this operator; shared/CI state remains pending.

The released image uses registry `bankeriqlive415eb635images.azurecr.io`, repository `runtime`,
and this immutable digest:

```text
sha256:842393e2bb8b82873541e62ef81b6cd15f148251a477de8db4fb78a793a7cc5c
```

Historical activity-only publication commands, before the semantic-memory release:

```sh
live_registry=bankeriqlive415eb635images.azurecr.io
az acr login --name "${live_registry%%.*}"
docker buildx build --platform linux/amd64 --load -f infra/azure/Dockerfile \
  -t "$live_registry/runtime:activity-20261103" .
docker push "$live_registry/runtime:activity-20261103"
az containerapp job start -g bankeriq-live-runtime -n bankeriq-li-c19afe58-migration
az containerapp job execution list -g bankeriq-live-runtime \
  -n bankeriq-li-c19afe58-migration -o json
```

The historical activity image used this digest:

```text
sha256:43b6231b1755f3f82c1f7a348f954d025a3259d059f69a95943eb2ceb19b57b4
```

## Initial semantic memory release (historical)

The user authorised an in-place application upgrade on 3 November 2026. The existing
resource group, database, Redis, identities, network and original conversation are retained.
Migration 013 adds semantic summary/operation/retrieval tables and runtime grants. The
normal queued application always enables compaction and scoped `retrieve_history`.
No per-conversation flag or separate summarization service is required.

The context window is 8,192 estimated tokens including 512 completion tokens; summary calls
use 8,192 including 2,048 completion tokens. The model selects source candidate identifiers;
trusted code restores exact quotations and attribution, then validates durable publication.
A shared process-local allowance of 8,500 estimated tokens per 65 seconds paces this one
coordinator's model traffic. Large conversations can wait for that allowance. It is not a
replica-wide quota controller and does not coordinate other users of the Foundry deployment.

The initial semantic image included the tokenizer cache, verified with networking disabled.
Its digest was `sha256:c3b422f8170105ef414e84dc966f39de6af9d977fa4224a1a0f3bea1c8c77f8c`.
It was built from this branch's uncommitted implementation. The earlier activity-only image
and its five-turn performance record remain historical evidence. The migration job image
was updated first using an intentionally targeted Terraform plan; the full application plan
then updates only the three app images. Both plans contain zero additions or deletions.

```sh
az acr login --name bankeriqlive415eb635images
semantic_image=bankeriqlive415eb635images.azurecr.io/runtime:semantic-20261103
docker buildx build --platform linux/amd64 --load -f infra/azure/Dockerfile \
  -t "$semantic_image" .
docker push "$semantic_image"
# Set runtime_image in runtime-live.tfvars to the inspected immutable digest above.
terraform -chdir=infra/azure plan -var-file=runtime-live.tfvars \
  -target='module.runtime[0].azurerm_container_app_job.migration[0]' \
  -out=semantic-migration.tfplan
terraform -chdir=infra/azure show semantic-migration.tfplan
terraform -chdir=infra/azure apply semantic-migration.tfplan
az containerapp job start -g bankeriq-live-runtime -n bankeriq-li-c19afe58-migration
az containerapp job execution list -g bankeriq-live-runtime \
  -n bankeriq-li-c19afe58-migration -o json
# Require a successful execution before applying the application plan.
terraform -chdir=infra/azure plan -var-file=runtime-live.tfvars -out=semantic-apps.tfplan
terraform -chdir=infra/azure show semantic-apps.tfplan
terraform -chdir=infra/azure apply semantic-apps.tfplan
```

The migration image update took 22 seconds; its execution succeeded in 37 seconds.
The three app image updates took 18–19 seconds; all reached ready revision `--0000001`.
The final Terraform plan reported no changes. Eighteen hosted turns completed, one summary
was published, an omitted source detail was retrieved, and scoped Redis-view loss recovered.
The original conversation and client file remain intact. The transcript records selection
quality limits and quota waits; these long-turn samples establish no latency percentile.
The [group-11 evidence](../../build-order/11-completed-semantic-compaction-and-retrieval.md)
records local acceptance and live conversation results. This release preserves the original
client file; its new synthetic conversation uses `scratch/group11-hosted-client.json`.

## Execution evidence and limits

Executed on 3 November 2026: `uv run python scripts/verify.py` passed **491 tests in
69.38 seconds, zero skips**, with real local PostgreSQL 17 and Redis 7. This includes
independent worker-process retention/restart tests and passive SSE expiry. Static format,
readability, types, JSON and 100-column checks passed. Terraform validation and ten mocked
plan tests passed. `git diff --check` passed. A final refreshed Terraform plan reported no
changes. Foundation creation took 10 minutes 16 seconds; the migration job resource took
22 seconds, migration execution took 38 seconds, and the three app resources took 19 seconds.
These timings exclude local implementation, image preparation and plan-review time.

Three hosted turns completed in the same conversation:

| Turn | Admission | Complete response | Observed result |
| --- | ---: | ---: | --- |
| Seed fictional codeword and number | 1.810s | 2.492s | Confirmed amber-lantern and 42 |
| Ask for the earlier values | 0.159s | 2.511s | Recalled both from conversation history |
| Calculate 17 + 25, prepare report | 0.163s | 5.835s | Total 42, two tools, separate delivery |

Timings cover the HTTP client through final history retrieval; token acquisition occurs
before timing starts. These three samples do not establish latency percentiles or an SLO.
The report body marker was absent from returned history. Both tool children completed
on attempt one. PostgreSQL held three runs, ten journal events and fourteen completed jobs.

Live transport checks returned 401 without authentication, 400 for blank input and 404
for unavailable conversation scope on history/activity/inspection. Replaying the final
request returned 202 with unchanged durable event, run and job counts. During 3.892 seconds
of open SSE plus status/history polling, history and activity TTLs fell from 1,799,953 ms
to 1,796,061 ms. The progress key was absent; this harness does not emit token deltas.
The 30-minute configured TTL was preserved throughout the hosted test.

The client file retains conversation `bd00e401-e135-4eaa-b221-76b886310cfe`. Local synthetic
evidence is under ignored `scratch/live-turn-{1,2,3}.json`, `live-passive-probe.json`,
`live-inspection.json` and `live-lifecycle-logs.json`. Reproduce normal chat/inspection with
the commands above. Compare two inspection results around `read`/`status` calls to observe
the declining TTL; `send` deliberately starts a new activity window.

Immediate console inspection found 20 API events, 31 coordinator events and seven tool events.
These included all twelve worker claims/starts/settlements, four admission receipts including
the duplicate, and cache reconstruction/hits. Message text, restricted body markers and bearer
token markers were absent from these inspected console records. Log Analytics subsequently
returned all 58 events, matching each service's console event counts; the same payload markers
were absent. Initial batches took several minutes to become queryable, and the ingestion cap
was not reached. Use the console stream for immediate inspection and the saved-log query for
later correlation; no loss-free logging SLO is claimed.
The one-to-two-hour idle conversation experiment requires elapsed wall time after setup;
shortened local TTL tests do not establish that endurance result.

This deployment uses synthetic arithmetic/report tools and bounded model output. It has
no browser UI, production autoscaling, PostgreSQL HA standby or journal deletion policy.
Its configured Log Analytics daily cap is not a total infrastructure spending limit.
No cleanup timer is installed: the environment is intentionally left alive for the user.

When the user explicitly requests teardown, review and apply a destroy plan with the same
variables and state, then verify the group and application are absent. Do not run these
commands merely because validation finished:

```sh
terraform -chdir=infra/azure plan -destroy -var-file=runtime-live.tfvars -out=live-cleanup.tfplan
terraform -chdir=infra/azure show live-cleanup.tfplan
terraform -chdir=infra/azure apply live-cleanup.tfplan
```

[log-delay]: https://learn.microsoft.com/en-us/azure/container-apps/log-options


## Automatic compaction and responsive admission release

On 3 November 2026, Terraform increased the existing Standard model deployment from 10 to
50 quota units (10,000 to 50,000 configured tokens per minute). The reviewed saved plan changed
only `azurerm_cognitive_deployment.coordinator` in place; application images and data resources
were unchanged by that apply. The apply completed at approximately 21:50 UTC.
Initial inference headers still advertised 10,000 TPM after management reported capacity 50.
Microsoft documents [up to 15 minutes for quota changes to propagate][quota-propagation].
Fresh inference responses at approximately 22:06 UTC advertised 50,000 TPM and 300 RPM.
The application has no artificial rolling-minute wait. A genuine HTTP 429 causes a durable
bounded retry deadline and releases the worker; available capacity still bounds sustained load.

The first `compaction-fast-20261103` image used this digest:

```text
sha256:257b983ac6a2351f5a9e6aed7f5927b7904f9a02dc9fa16a8f89288b49d2f729
```
 Migration job image
update took 20 seconds; execution `bankeriq-li-c19afe58-migration-8xpbsz9` succeeded from
22:15:32 to 22:16:05 UTC (33 seconds), applying additive migrations 014–015 and runtime grants.
The reviewed full plan updated only three app images, in 17–18 seconds. All roles reached
ready revision `--0000002`; the API emitted `runtime.ready` at 22:17:04 UTC. Control-plane
apply completion precedes application readiness. The final refreshed plan had no changes.

This image keeps two recent turns when the request budget allows, triggers compaction at
20% free headroom and targets 40%. Summary input/output share an 8,192-token estimated budget
with 1,024 reserved output tokens. Main inference keeps its 8,192/512 budget. Source-checked
structured summaries, explicit corrections, retained active user input and durable one-time
context-overflow recovery are enabled by normal queued startup.

Numeric `model.request` events now report adapter duration, actual usage, HTTP status and
provider quota headers. `memory.assembled` and `memory.compacted` report local token estimates.
These observations contain no prompts, responses, credentials or raw provider errors.
Application Insights and production alerting remain separate work.

The user's original conversation remains unchanged: five runs, 14 journal events, 18 completed
jobs and cursor 21. Separate synthetic chats exercise the new image. Its release evidence and
measurement limits are recorded in the group-11 and performance guides.

[quota-propagation]: https://learn.microsoft.com/azure/foundry/openai/how-to/quota

The subsequent `compaction-identity-final-20261103` image preserved user identities.
The final `compaction-assertions-20261103` image is the current digest above. It
preserves explicit user name/ownership excerpts after a hosted test exposed nickname omission.
The reviewed plan changes only the migration-job and three app images. No additional migration
is needed; the database is already through 015. Earlier synthetic trials are retained with their
fidelity failures. Existing user conversations and data resources remain intact.

The final image additionally preserves explicit user confirmations/corrections. All three roles
reached revision `--0000004`; image updates took 19 seconds and the migration-job image update
20 seconds. No extra schema change was needed. A trial request crossed old/new worker profiles
during rollout and failed visibly with `memory_profile_changed`; no output or source was lost.
Wait for old coordinator/tool revisions to have zero replicas and `Stopped` state before sending
validation traffic after a profile-changing release. `latestReadyRevisionName` alone does not
establish that old queue consumers have drained. Prepared work remains pinned and incompatible
workers fail closed. A general zero-downtime migration between incompatible runtime profiles
requires a separately designed rollout/drain procedure; no such guarantee is claimed here.
