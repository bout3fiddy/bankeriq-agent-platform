# Azure infrastructure

Terraform configuration and provisioning inventory for the Foundry deployment.
The initial configuration defines the local-inference environment. Hosted-agent
resources and shared state storage remain later phases.
Create resources through Terraform as required by [AGENTS.md](../../AGENTS.md).

The first milestone is a local coordinator calling a deployed Foundry model.
Hosted execution and the remaining platform services follow separately. Logical
responsibilities are described in [components.md](../../components.md).

## First model test

| Resource | Purpose and timing |
| --- | --- |
| Resource group | Groups the environment's Foundry resources for access control and lifecycle management. |
| Foundry account | An `AIServices` account with project management enabled and a custom subdomain. Provides the model endpoint. |
| Model deployment | Makes a selected model/version available under the deployment name used by the coordinator. Configure its deployment type and capacity explicitly. |
| Foundry project | Organises the future hosted agent and its connections. Create with the Foundry foundation; direct Chat Completions calls use the account endpoint. |
| Identities and role assignments | Give deployment operators and runtime identities the scoped access they need. Exact roles depend on the selected authentication and hosting path. |

Use AzureRM for supported resources and AzAPI where Foundry features require it.
Microsoft documents both providers for the account, project and model deployment.
See the [Foundry Terraform guide](https://learn.microsoft.com/en-us/azure/foundry/how-to/create-resource-terraform).

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
`az login` session. Managed identity integration remains application work.
Keep keys out of source, normal Terraform outputs and generated configuration files.

## Run the initial configuration

The defaults are West Europe, `gpt-4.1-mini` version `2025-04-14`, EU Data Zone
Standard, and 10 capacity units. `deployment_type` also accepts regional
`Standard`; global and provisioned deployment types are rejected. Microsoft's
[deployment types guide](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)
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
terraform -chdir=infra/azure output -json runtime_environment > infra/azure/runtime.json
mkdir -p scratch/transcripts
uv run bankeriq-runtime validate --provider foundry --config infra/azure/runtime.json \
  --transcript scratch/transcripts/foundry.md
```

Role changes can take time to propagate. A failed live command exits nonzero;
investigate access, quota and deployment readiness before rerunning. The
[demo guide](../../src/bankeriq_agent_platform/demo/README.md) describes the exact
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

Once local inference works and the container is ready, add:

| Resource | Purpose |
| --- | --- |
| Azure Container Registry | Stores the coordinator image; supply an immutable image digest to the deployment. |
| Project registry connection and scoped identity roles | Let Foundry pull the image and access the deployed model. |
| Guardrail policy | Supplies the account-level policy referenced by the hosted agent. |
| Hosted-agent definition | Registers the image and runtime configuration in the Foundry project through AzAPI's data-plane resource. |
| Monitoring resources and diagnostic settings | Collect approved operational events; telemetry fields and retention need an explicit application policy. |

Build and publish the image as an application release step. Terraform manages
the registry, connections and hosted-agent definition. Verify the resulting agent
version is ready before invoking it. Microsoft's
[hosted-agent Terraform guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-terraform)
describes the resource dependencies and deployment sequence.

Live identity propagation, input admission, networking and Python/container
compatibility still need validation before hosting this runtime.

## Later platform phases

These resources become necessary as their corresponding components are built:

| Component | Resources or decisions still needed |
| --- | --- |
| Conversation, run and approval state | A database with atomic updates; service selection and persistence adapter remain open. |
| Tool workers and durable execution | Worker compute and, if required, a queue/orchestrator. Transport and recovery design remain open. |
| Reports | Private Blob storage, worker/delivery identities and an authenticated backend that rechecks access on each download. |
| Browser application | Frontend/backend hosting and enterprise identity integration; hosting choices remain open. |
| Network and secret management | Network boundaries, private endpoints/DNS and Key Vault where required by the chosen services and credential flow. |

The self-hosted specialist is deferred. Current tests use synthetic tools,
in-memory state and mocked continuations. Provision each later service alongside
its implementation and validation.

## Provisioning workflow

Provider versions are recorded in `.terraform.lock.hcl`. Run formatting and
validation, generate a saved plan, review
its resources and settings, then apply it within the user's deployment
authorisation. Verify resource outputs and run the synthetic live inference test.
Keep Azure-specific deployment choices here and provider-neutral runtime code
under `src/agent_runtime/`.

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
prescribe the tool sequence, so this evidence evaluates workflow execution.
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
Microsoft's [model lifecycle guidance](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements).
