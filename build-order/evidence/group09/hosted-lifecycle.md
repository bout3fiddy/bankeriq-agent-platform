# Group 09 hosted lifecycle — 3 November 2026

Status: completed. Container Apps hosted separate API, coordinator and tool processes;
regional Foundry supplies inference. The workload contains only two fixed synthetic
requests. This is a disposable functional rehearsal, not a production capacity result.
The [foundation review](foundation-review.md) retains provisioning failures and early releases.

| Executed gate | Result and evidence |
| --- | --- |
| Authenticated arithmetic | One child, 3.39s; [result](hosted-initial.json). |
| Restricted report | Two children, 5.79s; separate [delivery checks](hosted-report.json). |
| Public model isolation | Public inference denied with [403](public-network.json). |
| Private connectivity | Private DNS/TLS; [probe](credentials-and-private-access.json). |
| Scoped SQL grants | Runtime DDL/prompt writes denied; same probe. |
| Token handling | Two new SQL token acquisitions and two Redis refresh callbacks; same probe. |
| Redis loss | Flush followed by durable cursor reconstruction; [result](redis-recovery.json). |
| Multiple replicas | Two concurrent delayed calls; [A](drain-a.json), [B](drain-b.json). |
| Scale-in | Two active leases before 2-to-1 rollout; [snapshot](drain-active.json). |
| Active SIGTERM | Active lease; [signal](active-signal.json), [drain](drain-log.json). |
| Worker restart | Three requests, one attempt each; [reconnect](restart-reconnect.json). |
| HA enablement | Primary zone 2, healthy standby zone 1; [state](ha-before.json). |

The six-process profile uses two replicas per role, two worker slots per process,
four connections per pool and queue bounds of four globally/two per tenant. The two
concurrent delayed calls completed in about 49s including a 45s synthetic tool delay.
A separate PID-1 SIGTERM during a 40s tool completed the request in 45.8s, emitted
`worker drained` and restarted once. Duplicate admissions did not increment child attempts.

Credential checks exercise fresh physical SQL connections and accelerated Redis token
refresh/re-auth callbacks. Azure may return a cached valid token. Natural token expiry
has not been soaked, and this evidence is not a 24-hour credential endurance test.
The six completed synthetic runs precede the [restore point](restore-ledger.json).
No new admission is made between that point and restore reconciliation.

## Reproduction

Use the reviewed foundation/release sequence in the [harness guide](../../../tests/azure/README.md).
The image, endpoint, audience, app and resource-group names are Terraform outputs.
For this execution, `pilot_group=bankeriq09-runtime`, `pilot_prefix=bankeriq09-415eb635`.
The CLI token stays in memory; evidence contains synthetic IDs and bounded counts.

```sh
az containerapp exec -g "$pilot_group" -n "$pilot_prefix-api" \
  --command 'python -m tests.azure.probe refresh'
az containerapp exec -g "$pilot_group" -n "$pilot_prefix-api" \
  --command 'python -m tests.azure.probe redis-reset'
uv run python -m tests.azure.reconnect --url "$pilot_url" \
  --audience "$pilot_audience" --source build-order/evidence/group09/drain-a.json \
  --source build-order/evidence/group09/drain-b.json \
  --source build-order/evidence/group09/active-sigterm.json --output scratch/reconnect.json
```

For active drain, run `python -m tests.azure.drain` through the tool app's exec channel,
then admit a synthetic request with a configured 40s tool delay. The guard only signals
PID 1 in this disposable tool container after it observes a leased tool job. The
actual first run used the same script through an interactive shell before it was
included in the image. Long inline exec commands exceeded Azure's URL limit.

## Verification checkpoint

The complete quality command passed **478 tests in 56.57s**, with zero skips, using
local PostgreSQL 17 and Redis 7. All nine mocked Terraform plan tests passed. Static
checks and relative Markdown targets passed. Provider versions are AzureRM 4.81.0,
AzureAD 3.10.0 and AzAPI 2.13.0; Python is 3.14 with locked dependencies.

HA enablement completed in 10m10s. The reviewed failover plan creates only the explicit
AzAPI restart action and its stable rehearsal ID. No database replacement is planned.
Failover, PITR, throttling and cleanup evidence will be appended after execution.

```sh
terraform -chdir=infra/azure plan -input=false -var-file=runtime-pilot.tfvars \
  -out=runtime-ha.tfplan
terraform -chdir=infra/azure show -json runtime-ha.tfplan
terraform -chdir=infra/azure apply -input=false runtime-ha.tfplan
# Add runtime_failover_id="group09-ha-1"; start the observer before applying.
terraform -chdir=infra/azure plan -input=false -var-file=runtime-pilot.tfvars \
  -out=runtime-failover.tfplan
terraform -chdir=infra/azure show -json runtime-failover.tfplan
terraform -chdir=infra/azure apply -input=false runtime-failover.tfplan
```


## Managed HA failover

The primary moved from zone 2 to zone 1. An authenticated observer made 703 successful
reads, saw one transport failure and 63 unavailable responses, then recovered the same
completed request. Its longest observed failure interval was **44.12s** over 449.15s.
The pilot maps a failed owned-status lookup to 404; that response here reflected the
brief database outage. The observer stopped after stable client recovery while Azure
continued recreating the standby.

[Observation](failover-observation.json), [request reconciliation](failover-reconnect.json)
and a fresh [database probe](failover-database.json) preserve the executed evidence.
The new private writer address is 10.90.2.5, with verified TLS. All six runs, seven joined
groups and seven tools remained completed. Three individually tracked admissions still
had one tool attempt each and unchanged revisions after duplicate delivery.
This tests a planned failover with completed requests and continuous authenticated reads;
it does not claim an unplanned regional outage or unknown-commit workload benchmark.

Azure reported the standby healthy again after **7m7s** of control-plane processing;
[final HA state](ha-after.json) records primary zone 1 and standby zone 2. Client
recovery and control-plane completion are measured separately.

## Point-in-time restore preparation

Azure restored PostgreSQL to `2026-11-03T11:32:35Z` in **7m10s**. It automatically
copied the migration identity's administrator assignment. AzureRM's following create
reported that the assignment already existed; its exact resource ID was imported into
the declared Terraform resource. No portal/CLI resource creation or identity change
was needed. The cutover plan pauses all three apps and updates the migration job to
the restored private endpoint with a fresh recovery UUID and immutable image.

```sh
terraform -chdir=infra/azure plan -input=false -var-file=runtime-pilot.tfvars \
  -out=runtime-restore.tfplan
terraform -chdir=infra/azure show -json runtime-restore.tfplan
terraform -chdir=infra/azure apply -input=false runtime-restore.tfplan
# pilot_restored_id and pilot_admin_id are the exact IDs returned by Azure/Terraform.
terraform -chdir=infra/azure import -input=false -var-file=runtime-pilot.tfvars \
 'module.runtime[0].azurerm_postgresql_flexible_server_active_directory_administrator.restored[0]' \
  "$pilot_restored_id/administrators/$pilot_admin_id"
```

The [migration](restore-migration.json) succeeded, advanced generation once and verified
that the old generation was fenced. The database contained no unresolved effects; local
real-PostgreSQL tests separately exercise refusal when such effects exist.
Cold API startup initially timed out two reconnect attempts; after readiness, the same
unchanged command passed. [Reconciliation](restore-reconnect.json) preserved all three
tracked requests and their single child attempts. [Database/cache checks](restore-database.json)
found all six completed runs/seven groups and rebuilt Redis from the private restored
writer at 10.90.2.7. Restore creation through successful reconciliation took **756.82s**
(12m37s), within the pilot's 30-minute target. No admission occurred after the selected
point before this reconciliation. This bounds the stated synthetic ledger, not a
production five-minute RPO or external-effect reconciliation process.

The restored apps use this immutable Linux amd64 image:
`sha256:e84c968a0b15e01abad0ed0bed79f090892249ceb5e6cf61f4801bbdc24d7056`.
The image's throttling module import and async managed-identity client construction
also passed inside the Linux container before deployment.

## Model throttling and scoped controls

The reviewed capacity plan changed only regional model capacity from ten units to one.
A coordinator request returned [HTTP 429](throttle-provider.json) with SDK retries disabled.
One fixed arithmetic input and its duplicate received 202; its [identity](throttle-admission.json)
remained stable. The [operator probe](throttle-inspect.json) found the inference job
stopped at exactly three attempts, with unknown outcome retained and no permitted retry.
A denied retry produced one idempotent audit receipt; a different tenant could not inspect it.
A later inspection confirmed the attempt count stayed at three. The run can remain
running while this stopped unknown outcome requires operator review. This rehearsal
preserves that existing conservative recovery behaviour and never clears its effect flag.

```sh
# Set model_capacity=1; review/apply the saved capacity-only Terraform plan.
az containerapp exec -g "$pilot_group" -n "$pilot_prefix-coordinator" \
  --command 'python -m tests.azure.throttle_probe provider'
# Admit the fixed input through the authenticated API, preserving its request UUID.
az containerapp exec -g "$pilot_group" -n "$pilot_prefix-coordinator" \
  --command 'python -m tests.azure.throttle_probe inspect'
# Restore model_capacity=10 through another reviewed Terraform plan.
```

The admission uses the same transport as `tests.azure.validate`: obtain the delegated
CLI token in memory, POST `/conversations`, set that conversation UUID and a fresh run
UUID in the headers, then POST `/runs` twice with identical `request_id` and
`text="Calculate the sum of 2 and 3."`. Assert both responses are 202. Run the operator
probe after retry exhaustion; the ordinary positive validator expects completion and
is therefore unsuitable for this negative test. The original execution used an inline
`uv run python` client with precisely these steps and wrote the admission ledger.

After restoring capacity, two fresh report validators still failed to complete. These
remain explicit failed observations: [first](post-quota-early-request.json) and
[second](post-quota-second-request.json). Inspection showed stopped inference jobs;
no exhausted budget was reset. A small direct provider probe succeeded, and pure
request preparation succeeded with 5,775 instruction characters and two tool definitions.
The [quota evidence](quota-propagation.json) isolated the cause: management reported
10,000 TPM while inference response headers still reported 1,000 TPM. Microsoft's
[quota guide][quota] allows up to 15 minutes for allocation propagation. Release checks
must confirm the effective inference limit before admitting full coordinator prompts.

[quota]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota

The management update completed at 12:06:47 UTC. A bounded diagnostic subsequently
sent only `Reply OK.` with 16 output tokens, zero SDK retries and 30-second intervals.
It inspected `x-ratelimit-limit-tokens`; no full coordinator request was retried during
that wait. Use the coordinator's interactive exec shell for this diagnostic; Azure's
exec command parser does not reliably preserve a quoted `python -c` expression.

```python
from tests.azure.throttle_probe import *
credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
client = OpenAI(
    base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],
    api_key=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
    max_retries=0,
    timeout=15,
)
response = client.chat.completions.with_raw_response.create(
    model="coordinator",
    messages=[{"role": "user", "content": "Reply OK."}],
    max_completion_tokens=16,
)
print(response.headers["x-ratelimit-limit-tokens"])
client.close()
credential.close()
```

The closeout quality run again passed **478 tests in 55.31s**, with zero skips, using
the real local PostgreSQL/Redis containers. Terraform formatting/validation, JSON and
100-character checks and `git diff --check` also passed. The reviewed destroy plan
contains 51 tracked deletions belonging to this disposable pilot, including the source
and restored database, API registration and explicit failover action state.

The effective inference limit reached 10,000 TPM and was confirmed at 12:16:34 UTC,
**586.52s after the management apply completed**. A new [report request][confirmed]
then completed in **5.48s**, with two completed children, separate authenticated report
delivery, duplicate admission and restricted-output checks. Previously exhausted jobs
were left unchanged. The 51-delete reviewed Terraform plan was then applied for teardown.

[confirmed]: restored-confirmed-report.json

## Teardown correction

The first destroy removed application, model, registry, Redis and identity resources,
but Azure rejected removal of the restored administrator with `AadAuthPrincipalDropFailed`
(SQLSTATE `2BP01`): the role still owned objects in the inherited `bankeriq` database.
Terraform continued polling; the Azure Activity Log exposed the actual failed operation.
The apply was interrupted gracefully and its partial state retained.

The inherited database was imported into a Terraform database resource. Both primary
and restored databases now explicitly depend on their administrator, which reverses
the teardown order: database first, administrator second, server afterward. Restore
creation now precedes declaring/importing its inherited children at cutover. A tenth
mocked Terraform test checks that staging. Static instance keys also permit imports
after partial teardown, without recreating deleted network or identity resources.
A second reviewed destroy plan resumes removal of only the remaining resources.

```sh
terraform -chdir=infra/azure import -input=false -var-file=runtime-pilot.tfvars \
  'module.runtime[0].azurerm_postgresql_flexible_server_database.restored[0]' \
  "$pilot_restored_id/databases/bankeriq"
terraform -chdir=infra/azure plan -destroy -input=false -var-file=runtime-pilot.tfvars \
  -out=runtime-destroy-final.tfplan
terraform -chdir=infra/azure show -json runtime-destroy-final.tfplan
terraform -chdir=infra/azure apply -input=false runtime-destroy-final.tfplan
```

The final destroy graph was inspected before apply: the administrator's destroy node
waits for the restored database's destroy node. The revised plan has **18 deletions**
and no creates/updates. After the Terraform changes, the complete quality command
passed again: **478 tests in 55.33s, zero skips**, plus **10 mocked Terraform tests**.
The temporary local PostgreSQL/Redis containers were removed after this final run.

Azure also automatically created `NetworkWatcherRG` and `NetworkWatcher_swedencentral`
at 10:40 UTC during pilot provisioning. The subscription had no groups beforehand;
inspection found only that regional watcher in this group. Both were imported into the
separate [networking cleanup root](../../../infra/azure/automatic-networking/README.md).
Its reviewed destroy plan is restricted to those two resources. The main profile does
not automatically adopt shared subscription networking. The Container Apps managed
`ME_...` group remains owned by its environment and is verified absent after deletion.

The watcher and its group were deleted successfully, with empty cleanup state and
`az group exists --name NetworkWatcherRG` returning false. The Container Apps managed
group also disappeared, but the environment stayed `ScheduledForDelete` after repeated
accepted deletion requests. Azure's Activity Log reported no failed environment delete.

Microsoft's [documented workaround][stuck] removes the associated VNet first. Inspection
confirmed the pilot VNet contained only its empty compute subnet, with no IP configurations
or service association links. After gracefully stopping the waiting apply, the VNet was
imported into the cleanup root; its VNet/subnet addresses were removed from the main
state only after import succeeded. State backups were retained. The reviewed cleanup
plan contains exactly one VNet deletion; all actions still use Terraform providers.

```sh
terraform -chdir=infra/azure/automatic-networking import -var-file=cleanup.tfvars \
  'azurerm_virtual_network.stuck_environment[0]' "$pilot_vnet_id"
terraform -chdir=infra/azure state rm 'module.runtime[0].azurerm_subnet.compute' \
  'module.runtime[0].azurerm_virtual_network.runtime'
terraform -chdir=infra/azure/automatic-networking plan -destroy \
  -var-file=cleanup.tfvars -out=vnet-cleanup.tfplan
terraform -chdir=infra/azure/automatic-networking show vnet-cleanup.tfplan
terraform -chdir=infra/azure/automatic-networking apply vnet-cleanup.tfplan
```

[stuck]:
  https://learn.microsoft.com/en-us/azure/container-apps/troubleshooting#manually-delete-the-vnet-being-used-by-the-azure-container-apps-environment

The transferred VNet deleted successfully in 11s and the cleanup root returned to
empty state. A fresh main-root destroy plan contains only the environment, Log Analytics
workspace and pilot resource group (three deletions). No workloads or data services
remained while this last control-plane deletion completed.

## Verified cleanup and closeout

After VNet removal, the environment deleted in 3m42s, the operations workspace in 12s
and the pilot resource group in 1m16s. The final apply completed with three deletions
and no creates or updates. Both Terraform states are empty. Live inspection returned
zero active subscription resource groups/resources and zero matching pilot Entra
applications; this also confirms absence of the Container Apps managed group and
automatic Network Watcher group. The [cleanup receipt](cleanup.json) records these checks.
No user login, consent, quota request or support intervention was needed.

```sh
terraform -chdir=infra/azure plan -destroy -input=false -var-file=runtime-pilot.tfvars \
  -out=runtime-last-destroy.tfplan
terraform -chdir=infra/azure show runtime-last-destroy.tfplan
terraform -chdir=infra/azure apply -input=false runtime-last-destroy.tfplan
terraform -chdir=infra/azure state list
terraform -chdir=infra/azure/automatic-networking state list
az group list --query '[].name' -o json
az resource list --query '[].id' -o json
az ad app list --filter "appId eq '$pilot_client_id'" --query 'length(@)' -o json
```

Final checks cover Terraform formatting/validation for both roots, Markdown targets,
JSON/source line limits and `git diff --check`. The final Python verification remains
478 passed in 55.33s with zero skips; only Terraform cleanup and documentation changed
after that run. Ten mocked Terraform tests passed. Owned local test containers were
removed and the disposable registry's Docker login was cleared. Soft-deleted recovery
records were not purged, subscription provider registrations remain enabled and final
billing settlement was not measured. Shared state, production load/SLO validation,
natural token-expiry endurance and Foundry Hosted Agents deployment remain separate work.
