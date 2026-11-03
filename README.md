# Agent platform

A design and development scaffold for an Azure-hosted banking assistant. This is
an exploratory design inspired by the role discussion, not Rabobank's confirmed
architecture.

The interface will be a dedicated authenticated browser chat app, with React a
candidate frontend. Teams is outside the proposed delivery boundary.

The proposed general-purpose external coordinator runs an application-owned loop
using Microsoft Foundry inference and discovers tools allowed by the user's
permissions. Azure Container Apps is selected for the group-09 queue-worker pilot;
the earlier Foundry Hosted Agents target remains a separate unvalidated hosting path.
Visible tool outputs return to the coordinator. Restricted outputs
stay with an internal specialist or containerised application, with reports
delivered directly to the user through authenticated downloads.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and security boundaries.
See [components.md](components.md) for logical responsibilities and initial scope.
Foundry remains the current provider. [Provider selection](src/agent_runtime/providers/README.md)
keeps model access and hosting independent of the core runtime, with separate
startup settings and optional SDK dependencies.

## Source layout

- [`src/agent_harness/`](src/agent_harness/README.md): model/tool decisions, context and
  memory policy, prompt compilation, permissions/output enforcement and interfaces.
- [`src/agent_runtime/`](src/agent_runtime/README.md): durable workers, PostgreSQL/Redis,
  model/hosting adapters, sessions, recovery and telemetry.
- [`src/agent_platform/`](src/agent_platform/README.md): backend API, Entra identity,
  application configuration, provider registration and composition.
- [`frontend/`](frontend/README.md): reserved browser application; no UI is implemented yet.
- `infra/azure/`: Terraform-managed infrastructure. Package moves do not deploy changes.

The platform imports runtime and harness; runtime imports harness. The harness remains
independent of clients, credentials, networking and scheduling. The Python distribution is
`agent-platform` and contains all three packages. Existing prompt IDs, environment variables,
database formats and cloud resource names retain their compatibility identities.
See the [package refactor record](docs/package-layout.md) for compatibility checks,
executed validation and deployment limits.

## Current scope

The initial runtime scope is complete as of 2 November 2026, including live
Foundry inference with synthetic tools and in-memory state. The
[build order](build-order/README.md) separates this completed milestone
from subsequent infrastructure milestones and later runtime features.

The complete `build-order/` folder is local and gitignored, including plans and evidence.
Its links work in checkouts that contain those local files. Fresh clones use the tracked
architecture, component guides and performance register until the roadmap is supplied.

This repository contains design documentation, core runtime data types, a
permission-filtered tool registry, completed-output enforcement, a synchronous
tool gateway, conversation/run state, an in-memory store with atomic checkpoint
updates, a bounded coordinator loop and `src/agent_harness/continuations/` semantics
for job and approval events. Scripted model replies exercise repeated
tool calls and resumption. The [integration harness](tests/integration/README.md)
connects these components through synthetic calculation, approval, report and
delivery workflows, including failure and permission changes.
The [Foundry model adapter](src/agent_runtime/providers/foundry/README.md) implements v1
Chat Completions calls through the OpenAI SDK and is tested with a local HTTP
transport. Live inference passed on 2 November 2026 using a Terraform-provisioned
regional `gpt-4o` deployment in Sweden Central; see the
[validation evidence](infra/azure/README.md#validation-evidence).
The [hosting boundary](src/agent_runtime/hosting/README.md) exposes conversation
creation, run start and continuation through Microsoft's Invocations SDK, tested
locally with synthetic identity and input policy. Its worker pool allows separate
sessions to execute concurrently while each coordinator loop remains synchronous.
Python 3.14 is the tooling and container baseline. The synthetic container has
passed local Linux ARM64 validation. That Foundry Hosted Agents lifecycle remains a
separate hosting path from the Container Apps pilot.

The [executable development profile](tests/validation/README.md)
wires these components with synthetic tools and in-memory state. Run
`uv run python -m tests.validation validate` for the complete local workflow, or select
Foundry explicitly for live inference. [Azure Terraform](infra/azure/README.md)
defines the development resources; repeat live validation after provisioning a
supported model with available quota.

The [Redis session and history adapter](src/agent_runtime/sessions/README.md)
adds disposable UI views, provisional input reconciliation, durable acceptance,
bounded asynchronous history projection and optional immutable prompt caching.
Accepted messages, explicit activity signals and live-worker maintenance renew Redis
retention. Passive history/status/SSE reads allow idle expiry; PostgreSQL retains the
conversation. The [persistent test deployment](infra/azure/LIVE.md) supports operator chat
and metadata-only backend inspection.
The [performance register](performance/README.md) records proposed latency targets,
dated measurements, measurement gaps and the procedure for refreshing evidence.
The [durable synthetic workers](src/agent_runtime/workers/README.md) add
leased execution, restart reconstruction, atomic settlement and bounded maintenance.
Worker recovery also exposes scoped failure inspection, bounded crash recovery and
idempotent operator controls with durable audit decisions.
Local tests use real PostgreSQL/Redis and independent worker processes. Real
asynchronous coordinator/tool execution is implemented in the
[queued profile](src/agent_runtime/workers/asynchronous.md).
[Recovery and scaling validation](build-order/08-completed-recovery-and-scaling-validation.md)
adds bounded primary pooling, eligible-backlog observations, progress isolation and
executed local outage/restore and 2,800-job load evidence.
The [semantic memory profile](src/agent_runtime/memory/README.md)
adds source-linked PostgreSQL summaries, token-budgeted context and scoped history retrieval.
It is required by queued startup and enabled in the persistent Azure test deployment.
Automatic headroom checks retain a bounded verbatim tail, preserve explicit source facts,
compact completed prefixes of long turns and permit one durable context-overflow retry.
The synchronous in-memory profile remains unchanged.
[Azure deployment](build-order/09-completed-azure-runtime-deployment.md) is complete for
the synthetic Container Apps pilot, with delegated Entra admission, private services,
worker drain/restart, HA failover, fenced restore and model-throttling evidence.
The disposable resources were removed through Terraform after validation.

## Development

Install [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync --locked
uv run python scripts/verify.py
uv run pre-commit install
```

The verification command checks the lockfile, formatting, lint, paragraph and
comment spacing, and types. It runs pytest when test files exist and explicitly reports when none
exist. Pre-commit runs static checks. See [STYLEGUIDE.md](STYLEGUIDE.md) and the
[test layout and commands](tests/README.md).

## Next step

The [synthetic container](tests/validation/README.md#container-validation)
packages the existing composition with locked dependencies and non-root execution.
Local Docker checks cover readiness, two HTTP turns and graceful shutdown.

The selected infrastructure is PostgreSQL for conversations and durable job queues,
with Redis serving active conversation views and ephemeral progress. Accepted turns
and checkpoints use small durable journal writes; large history snapshots are
materialised asynchronously and on idle. Independent tool calls run through worker
queues while the coordinator releases its execution slot. These integrations are
implemented in the queued runtime. The
[PostgreSQL journal adapter](src/agent_runtime/storage/README.md) now persists
ordered admission, checkpoints, synthetic job records, results and approvals,
with local PostgreSQL failure tests. The default executable retains in-memory
state; application composition can inject the adapter. Redis sessions, asynchronous
history and synthetic durable workers are implemented. See the
[state ownership and session lifecycle](ARCHITECTURE.md#selected-state-and-execution-services).

The [shared prompt system](src/agent_harness/prompts/README.md) now resolves a
published application/environment binding at each new model step and records its
manifest before inference. Prepared steps retain their original version across
recovery. Administration supports publication, activation, rollback and withdrawal;
Foundry receives request-scoped instructions. Synthetic validation uses the same
runtime/system catalog as application startup. Business overlays remain later work.

Tools own private Blob reports and authenticated download endpoints. The runtime
forwards opaque links to the user without reading report contents. Tool-specific
storage and delivery implementations remain separate work. Infrastructure is
managed through Terraform; the hosted synthetic runtime is validated. Production identity
and general chat-input admission, the browser app and self-hosted specialist remain deferred.

Prompt authoring uses [versioned JSON definitions](prompts/README.md), with separate
runtime and BankerIQ system roots composed from reusable rules and guidelines.
Validate them with `uv run python -m agent_platform.prompt_files validate`.
