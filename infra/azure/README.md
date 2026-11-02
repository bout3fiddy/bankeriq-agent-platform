# Azure infrastructure

Provisioning inventory for the Foundry deployment. This folder currently contains
documentation only; Terraform configuration and deployment validation are pending.
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

The current startup factory reads `AZURE_OPENAI_API_KEY`. Azure CLI login supplies
deployment credentials; runtime authentication still needs wiring. Entra token
acquisition and managed identity integration remain application work. Keep keys
out of source, normal Terraform outputs and generated configuration files.

## Terraform state storage

Before shared or CI-managed deployment, provision a dedicated state resource
group, Storage Account, private Blob container and scoped backend-access role
assignments. Use Entra authentication and enable recovery controls for state.
The [Azure backend](https://developer.hashicorp.com/terraform/language/backend/azurerm)
supports Blob-backed state and locking.

Bootstrap this storage with Terraform using protected local state, then migrate
that state to the remote backend. Keep backend resources on a separate lifecycle
from disposable application environments. State and saved plans can contain
secrets and must stay out of Git. Add ignore rules before introducing Terraform
files or running it in this repository.

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

When Terraform configuration is added, pin provider versions and commit the
provider lockfile. Run formatting and validation, generate a saved plan, review
its resources and settings, then apply it within the user's deployment
authorisation. Verify resource outputs and run the synthetic live inference test.
Keep Azure-specific deployment choices here and provider-neutral runtime code
under `src/agent_runtime/`.

Source documentation checked on 2 November 2026. No Terraform plan or apply has
been run for this inventory.
