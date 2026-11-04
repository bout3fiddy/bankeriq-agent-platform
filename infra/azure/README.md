# Azure infrastructure

Terraform configuration and provisioning inventory for Foundry and the group-09
Container Apps pilot. The default profile supports local inference; setting
`runtime_enabled=true` enables the [private hosted module](modules/runtime/README.md).
It defines PostgreSQL, Redis, registry, identities, Entra admission, compute and logs.
The immutable image and migration job precede enabling API/coordinator/tool apps.
The [pilot entry point](../../tests/azure/README.md) uses the existing durable queues.
Hosted lifecycle validation and disposable cleanup are complete; execution evidence
belongs to [group 09](../../build-order/09-completed-azure-runtime-deployment.md).
Create resources through Terraform as required by [AGENTS.md](../../AGENTS.md).
The separately authorised [persistent operator deployment](LIVE.md) stays running for
non-sensitive conversations and idle-cache inspection; its lifetime differs from the
completed disposable group-09 exercise.

Group-12 alert definitions reuse the existing workspace. Source defaults are disabled;
the retained deployment has four user-approved rules with `runtime_alerts_enabled=true`.
Optional existing notification routes use `runtime_alert_action_group_ids`; the retained
deployment uses Azure Monitor portal delivery without an action group. Read the [operator
runbook][operator-queries]
for thresholds, costs/ingestion boundaries and outstanding delivery validation.

[operator-queries]: ../../performance/queries/README.md

The original first milestone ran a local coordinator against a deployed Foundry model.
The hosted pilot adds the durable API/worker profile. Logical
responsibilities are described in [components.md](../../components.md).

## First model test

- **Resource group**
  Purpose and timing: Groups the environment's Foundry resources for access control and lifecycle
  management.

- **Foundry account**
  Purpose and timing: An `AIServices` account with project management enabled and a custom
  subdomain. Provides the model endpoint.

- **Model deployment**
  Purpose and timing: Makes a selected model/version available under the deployment name used by the
  coordinator. Configure its deployment type and capacity explicitly.

- **Foundry project**
  Purpose and timing: Organises the future hosted agent and its connections. Create with the Foundry
  foundation; direct Chat Completions calls use the account endpoint.

- **Identities and role assignments**
  Purpose and timing: Give deployment operators and runtime identities the scoped access they need.
  Exact roles depend on the selected authentication and hosting path.


Use AzureRM for supported resources and AzAPI where Foundry features require it.
Microsoft documents both providers for the account, project and model deployment.
See the [Foundry Terraform guide][line-source-1].

Choose the region, model/version, deployment type and capacity after checking
subscription quota, availability and processing-location requirements. The model
must support our synchronous text/function Chat Completions adapter. Explicit
reasoning settings require separate compatibility verification.

Terraform should output the resource IDs, project endpoint, inference base URL
and model deployment name. Map the latter two to `FOUNDRY_OPENAI_BASE_URL` and
`MODEL_NAME`; keep `MODEL_PROVIDER=foundry` in application configuration. See the
[provider guide](../../src/agent_runtime/providers/README.md).

The initial configuration disables API keys and assigns the deploying principal
the account-scoped `Cognitive Services OpenAI User` role. Runtime settings select
`FOUNDRY_AUTH_MODE=azure_cli`; the provider obtains renewable tokens from your
`az login` session. Foundry credentials also support explicit user-assigned managed identity; hosted
integration and refresh validation remain group-09 work.
Keep keys out of source, normal Terraform outputs and generated configuration files.

## Run the initial configuration

The defaults are West Europe, `gpt-4.1-mini` version `2025-04-14`, EU Data Zone
Standard, and 10 capacity units. `deployment_type` also accepts regional
`Standard`; global and provisioned deployment types are rejected. Microsoft's
[deployment types guide][line-source-2]
describes the EU data zone and Azure geography processing boundaries.

The approved regional test uses Sweden Central, `gpt-4o` version `2024-11-20`,
and Standard with 10 capacity units. Copy [sweden.tfvars.example](sweden.tfvars.example)
to the ignored `development.tfvars` for these settings. Both supported deployment
types use pay-per-token billing. Capacity controls rate limits; configure spending
controls separately. The account has an authenticated
public endpoint for local development. Check model availability and unused quota
before applying. A plan can succeed even when model quota is zero.

```sh
export TF_VAR_subscription_id="<your-subscription-id>"
terraform -chdir=infra/azure init
terraform -chdir=infra/azure fmt -check -recursive
terraform -chdir=infra/azure validate
terraform -chdir=infra/azure test
cp infra/azure/sweden.tfvars.example infra/azure/development.tfvars
terraform -chdir=infra/azure plan -var-file=development.tfvars -out=development.tfplan
```

Provider setup registers `Microsoft.CognitiveServices` if required. The reviewed
plan creates five resources: the resource group, account, project, model deployment
and inference role assignment. After quota and deployment authorisation are settled:

```sh
terraform -chdir=infra/azure apply development.tfplan
terraform -chdir=infra/azure output -json runtime_environment | \
  uv run python -m json.tool > infra/azure/runtime.json
mkdir -p scratch/transcripts
uv run python -m tests.validation validate --provider foundry \
  --config infra/azure/runtime.json \
  --transcript scratch/transcripts/foundry.md
```

Role changes can take time to propagate. A failed live command exits nonzero;
investigate access, quota and deployment readiness before rerunning. The
[validation guide](../../tests/validation/README.md) describes the exact
synthetic workflow and its limits.

Choose a new transcript filename for each run. The exporter preserves existing
files and records the supplied instructions, observed calls/results and final
answer. Transcripts are local synthetic test evidence under the ignored `scratch/`
folder; production conversation logging still requires its own access and retention
policy.

After a disposable test, review and apply the destruction plan using the same
variables:

```sh
test_resource_group=$(terraform -chdir=infra/azure output -raw resource_group_name)
terraform -chdir=infra/azure plan -destroy -var-file=development.tfvars -out=cleanup.tfplan
terraform -chdir=infra/azure apply cleanup.tfplan
az group exists --name "$test_resource_group"
```

The provider preserves the Cognitive Services account's soft-deleted recovery
record and service registration. The resource group, active account, project,
deployment and inference role assignment are removed. Local state, plan files
and runtime settings stay ignored by Git; the generated endpoint stops working
after cleanup.

Azure retains the account name while its recovery record exists. A repeat test
using AzureRM can fail with `FlagMustBeSetForRestore`. For another disposable
test, set a fresh `name_prefix` in the ignored `development.tfvars` before planning
and keep that same file for cleanup. For example, `name_prefix = "bankeriq-transcript"`
was used for the transcript capture run. This preserves earlier recovery records.

## Terraform state storage

Before shared or CI-managed deployment, provision a dedicated state resource
group, Storage Account, private Blob container and scoped backend-access role
assignments. Use Entra authentication and enable recovery controls for state.
The [Azure backend](https://developer.hashicorp.com/terraform/language/backend/azurerm)
supports Blob-backed state and locking.

Bootstrap this storage with Terraform using protected local state, then migrate
that state to the remote backend. Keep backend resources on a separate lifecycle
from disposable application environments. State and saved plans can contain
secrets and must stay out of Git. Ignore rules cover state, saved plans, local
variable files and generated runtime settings. The current local backend is for
this single-developer validation; migration to shared storage remains pending.

## Hosted coordinator phase

The earlier Foundry Hosted Agents target remains an unvalidated deployment path.
Group 09 selects Container Apps workers connected to Foundry inference. Preserve
the existing hosted-agent adapter and its tests. If Foundry hosting is retained,
validate queue polling, private connectivity, shutdown and PostgreSQL recovery
ownership before adding the following resources:

- **Azure Container Registry**
  Purpose: Stores the coordinator image; supply an immutable image digest to the deployment.

- **Project registry connection and scoped identity roles**
  Purpose: Let Foundry pull the image and access the deployed model.

- **Guardrail policy**
  Purpose: Supplies the account-level policy referenced by the hosted agent.

- **Hosted-agent definition**
  Purpose: Registers the image and runtime configuration in the Foundry project through AzAPI's
  data-plane resource.

- **Monitoring resources and diagnostic settings**
  Purpose: Collect approved operational events; telemetry fields and retention need an explicit
  application policy.


Build and publish the image as an application release step. Terraform manages
the registry, connections and hosted-agent definition. Verify the resulting agent
version is ready before invoking it. Microsoft's
[hosted-agent Terraform guide][line-source-3]
describes the resource dependencies and deployment sequence.

Live identity propagation, input admission, networking and Python/container
compatibility still need validation before hosting this runtime.

## Selected runtime infrastructure

The following inventory records the design selected on 2 November 2026. Its original
"still needed" entries are historical; the hosted module now implements the database,
cache, API/workers, registry, managed identities, private network and monitoring.
[Group 09](../../build-order/09-completed-azure-runtime-deployment.md) records executed
gates and limits. Tool-owned Blob reports, the browser application, shared state
and production autoscaling remain separate work. Regional availability, capacity and
SDK compatibility must be verified for each new deployment.

- **Durable persistence and queue**
  Resources or decisions still needed: PostgreSQL Flexible Server, database and scoped application
  roles. Application migrations define journal, history pages, checkpoints, jobs, leases, retries,
  approvals and projection cursors. Admission and next-job creation commit together.

- **Ephemeral sessions**
  Resources or decisions still needed: Azure Managed Redis, private endpoint and scoped access. Main
  API read path for active context, cursor-aware updates, provisional streams and idle expiry.
  Rebuild from PostgreSQL journal/snapshots after loss.

- **Shared system prompts**
  Resources or decisions still needed: Application migrations now implement immutable versions,
  active bindings and per-step manifests in the same PostgreSQL database; Redis caches immutable
  prompt bodies. Separate admin publication permissions from worker read access. No additional Azure
  prompt service is required.

- **API and workers, proposed**
  Resources or decisions still needed: Container Apps workload-profiles environment with Consumption
  compute; separate API, coordinator and tool applications, identities and bounded replica counts.
  Maintenance initially shares a worker role.

- **Runtime transport**
  Resources or decisions still needed: PostgreSQL job rows and short transactional claims.
  LISTEN/NOTIFY is a wake-up optimisation with polling fallback. No DTS, Kafka or Service Bus
  resource in this design.

- **Autoscaling**
  Resources or decisions still needed: HTTP rules for the API; due-job metrics for workers. Validate
  Container Apps support/authentication for the KEDA PostgreSQL scaler; begin with fixed bounded
  replicas if unavailable.

- **Images and release**
  Resources or decisions still needed: Container Registry, managed image-pull identity, immutable
  digests and compatible worker revisions during drain/migration.

- **Tool-owned reports**
  Resources or decisions still needed: Private Blob storage, producing-tool identity and
  authenticated report endpoint belong to each tool's implementation. Runtime forwards opaque links
  without report-read permission; report resources are outside this phase.

- **Browser application**
  Resources or decisions still needed: Frontend/backend hosting and enterprise identity integration;
  hosting choices remain open.

- **Network and secret management**
  Resources or decisions still needed: VNet/subnets, private PostgreSQL connectivity, Redis private
  endpoint/DNS, egress controls and Key Vault where required. User-facing API ingress authenticates
  with Entra; queues and storage are internal.

- **Monitoring and recovery**
  Resources or decisions still needed: Azure Monitor/Log Analytics with approved fields, DB
  backups/PITR, retention settings and restore drills. General Purpose zone-redundant PostgreSQL HA
  is a production sizing decision; the synthetic pilot uses a single primary.


PostgreSQL owns accepted data and execution state. Redis serves active interaction;
large history snapshots are materialised asynchronously and on idle from a small
durable journal. Idle cleanup can discard Redis before snapshot completion because
the journal still reconstructs accepted turns. The
[architecture](../../ARCHITECTURE.md#selected-state-and-execution-services) specifies
ownership, async execution, proposed sizing and failure gates; the
[build order](../../build-order/README.md) tracks each integration separately.
The self-hosted specialist is deferred. Current tests use synthetic tools and
mocked continuations, with both in-memory and real local PostgreSQL storage. No additional resources
are provisioned
by this design selection. Terraform state Blob storage remains a separate
infrastructure responsibility from tool-owned report storage.

Use application connection pools for the pilot. Azure's built-in PgBouncer is
available on General Purpose/Memory Optimized tiers; LISTEN needs a session
connection. Managed identity is preferred, with scaler authentication and token
refresh verified against the actual deployed versions. Do not assume upstream
KEDA feature support establishes Container Apps support.

The planned [transaction and replication
boundaries](../../ARCHITECTURE.md#transaction-and-replication-boundaries)
use one writable PostgreSQL primary. All state/queue writes, unknown-commit lookups,
job ownership and active prompt resolution use its writer endpoint. Optional read
replicas are asynchronous and serve only explicitly lag-tolerant history/reporting;
current access/deletion checks still use the primary. Azure's HA standby supports
managed failover and is a separate role from those read replicas.

Before production provisioning, select recovery-point/time targets, HA failure
domains and restore procedures. Test writer reconnection and unknown commits during
HA failover. A restore or promotion that may lose commits must pause dispatch,
fence old workers, advance the recovery generation, rebuild Redis and reconcile
external effects before queues reopen. Define any HA/read replicas through
Terraform and review their plan and cost. The synthetic pilot provisions neither
through this design; sharding and broker resources remain deferred.
[Azure HA][line-source-4],
[read replicas][line-source-5]

Service references: [PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/overview),
[Azure Managed Redis](https://learn.microsoft.com/en-us/azure/redis/overview),
[Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app) and
[PostgreSQL scaler](https://keda.sh/docs/2.21/scalers/postgresql/).

## Provisioning workflow

Provider versions are recorded in `.terraform.lock.hcl`. Run formatting and
validation, generate a saved plan, review
its resources and settings, then apply it within the user's deployment
authorisation. Verify resource outputs and run the synthetic live inference test.
Keep Azure-specific deployment choices here and provider-neutral runtime code
under `src/agent_harness/`.

## Validation evidence

Executed on 2 November 2026:

- Terraform validation and five mocked plan tests passed, including rejection
  of non-EU locations, global deployment types and excessive capacity.
- Terraform created the five development resources in Sweden Central. The live
  model was `gpt-4o` version `2024-11-20`, deployment type `Standard`, capacity 10.
  Authentication used Azure CLI Entra credentials with API keys disabled.
- `bankeriq-runtime validate --provider foundry` passed: four model calls, three
  successive tool calls, one job continuation and a final answer containing 42.
  The validator checked that synthetic restricted content and report identifiers
  were absent from model history.
- A separately launched `serve --provider foundry` process passed a loopback TCP
  test: readiness, unauthenticated HTTP 401, unapproved-input HTTP 403, two complete
  conversation turns with report continuations, and graceful shutdown. Each turn
  returned 42. The transient bearer token was absent from server output.
- Terraform cleanup destroyed all five resources. Terraform state is empty,
  `az group exists --name bankeriq-dev-runtime` returned `false`, and the account
  was absent from the active account listing. The service registration and
  soft-deleted account recovery record are retained.

The repeat run at 12:03 UTC captured the full recorded conversation in
`scratch/transcripts/foundry-2026-11-02.md` using `--transcript`. It again passed
with four model calls, three tool calls and one continuation. Its final answer
was `The total is 42, and the report status is "restricted."` The receipt recorded
execution as completed and output as restricted; the final wording conflates
those two fields. The transcript retains the supplied instructions, which
prescribed the tool sequence, so this historical evidence evaluates workflow execution.
At that historical checkpoint, the normal BankerIQ prompt profile had not yet been
validated against a live model after removing those test-specific instructions.
The exporter and existing Python checks passed all 241 tests.
Terraform then destroyed all five repeat-test resources; the state is empty,
the `bankeriq-transcript-runtime` resource group no longer exists, and its
Foundry account is absent from the active account listing.

During repeat provisioning, Azure rejected simultaneous project/model creation
with `RequestConflict`. Terraform now orders model creation before project
creation; a retry completed successfully.

The coordinator ran locally. Tools, report artifacts, job completion and storage
were synthetic or in-memory; this did not deploy a hosted coordinator or test
production identity, durable workers or authenticated report downloads.

The quota check found zero usable Data Zone Standard chat-model capacity in
West Europe, Sweden Central, France Central, Germany West Central and Poland
Central. Regional `gpt-4o-mini` still showed quota but Azure rejected deployment
with `ServiceModelDeprecated`; its regional version retired on 31 March 2026.
The successful `gpt-4o` version was selected after inspecting both quota and
per-SKU retirement metadata. Check both again before repeating the test, following
Microsoft's [model lifecycle guidance][line-source-6].

[line-source-1]:
  https://learn.microsoft.com/en-us/azure/foundry/how-to/create-resource-terraform
[line-source-2]:
  https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types
[line-source-3]:
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-terraform
[line-source-4]:
  https://learn.microsoft.com/en-us/azure/postgresql/high-availability/concepts-high-availability
[line-source-5]:
  https://learn.microsoft.com/en-us/azure/postgresql/read-replica/concepts-read-replicas
[line-source-6]:
  https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements

## Automatically created networking

Azure may create a regional Network Watcher and `NetworkWatcherRG` when the pilot's
first VNet is provisioned. These can survive VNet deletion. The separate
[import-and-destroy root](automatic-networking/README.md) handles cleanup only when
inspection proves exclusive disposable-pilot ownership. Never adopt or delete a shared
watcher group. The Container Apps managed resource group follows its environment's lifecycle.
