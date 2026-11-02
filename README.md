# BankerIQ agent platform

A design and development scaffold for an Azure-hosted banking assistant. This is
an exploratory design inspired by the role discussion, not Rabobank's confirmed
architecture.

The interface will be a dedicated authenticated browser chat app, with React a
candidate frontend. Teams is outside the proposed delivery boundary.

The proposed general-purpose external coordinator runs an application-owned loop
using Microsoft Foundry inference and discovers tools allowed by the user's
permissions. Azure Container Apps is proposed for independently scalable queue
workers; the earlier Foundry Hosted Agents target remains a hosting compatibility
decision. Visible tool outputs return to the coordinator. Restricted outputs
stay with an internal specialist or containerised application, with reports
delivered directly to the user through authenticated downloads.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and security boundaries.
See [components.md](components.md) for logical responsibilities and initial scope.
Foundry remains the current provider. [Provider selection](src/agent_runtime/providers/README.md)
keeps model access and hosting independent of the core runtime, with separate
startup settings and optional SDK dependencies.

## Current scope

The initial runtime scope is complete as of 2 November 2026, including live
Foundry inference with synthetic tools and in-memory state. The
[build order](build-order.md) separates this completed milestone
from the planned infrastructure phase and later runtime features.

This repository contains design documentation, core runtime data types, a
permission-filtered tool registry, completed-output enforcement, a synchronous
tool gateway, conversation/run state, an in-memory store with atomic checkpoint
updates, a bounded coordinator loop and a `src/agent_runtime/durable/` scaffold
for mocked job and approval events. Scripted model replies exercise repeated
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
passed local Linux ARM64 validation; Azure hosting compatibility remains pending.

The [executable development profile](tests/validation/README.md)
wires these components with synthetic tools and in-memory state. Run
`uv run python -m tests.validation validate` for the complete local workflow, or select
Foundry explicitly for live inference. [Azure Terraform](infra/azure/README.md)
defines the development resources; repeat live validation after provisioning a
supported model with available quota.

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
being added on `codex/runtime-infrastructure`. The
[PostgreSQL journal adapter](src/bankeriq_agent_platform/storage/README.md) now persists
ordered admission, checkpoints, synthetic job records, results and approvals,
with local PostgreSQL failure tests. The default executable retains in-memory
state; application composition can inject the adapter. Redis sessions and asynchronous
history are next. See the
[state ownership and session lifecycle](ARCHITECTURE.md#selected-state-and-execution-services).

The [shared prompt system](src/agent_runtime/prompts/README.md) now resolves a
published application/environment binding at each new model step and records its
manifest before inference. Prepared steps retain their original version across
recovery. Administration supports publication, activation, rollback and withdrawal;
Foundry receives request-scoped instructions. Synthetic validation uses the same
runtime/system catalog as application startup. Business overlays remain later work.

Tools own private Blob reports and authenticated download endpoints. The runtime
forwards opaque links to the user without reading report contents. Tool-specific
storage and delivery implementations remain separate work. Infrastructure is
managed through Terraform; hosted runtime validation, production identity and input
admission are pending. The browser app and self-hosted specialist remain deferred.

Prompt authoring uses [versioned JSON definitions](prompts/README.md), with separate
runtime and BankerIQ system roots composed from reusable rules and guidelines.
Validate them with `uv run python -m bankeriq_agent_platform.prompt_files validate`.
