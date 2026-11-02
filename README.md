# BankerIQ agent platform

A design and development scaffold for an Azure-hosted banking assistant. This is
an exploratory design inspired by the role discussion, not Rabobank's confirmed
architecture.

The interface will be a dedicated authenticated browser chat app, with React a
candidate frontend. Teams is outside the proposed delivery boundary.

The proposed general-purpose external coordinator runs an application-owned loop
in Microsoft Foundry Hosted Agents and discovers tools allowed by the user's
permissions. Visible tool outputs return to the coordinator. Restricted outputs
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
[build order](src/agent_runtime/build-order.md) separates this completed milestone
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
Python 3.14 is the local tooling baseline copied from MONKI; deployment runtime
compatibility must be checked before selecting an Azure hosting target.

The [executable development profile](src/bankeriq_agent_platform/demo/README.md)
wires these components with synthetic tools and in-memory state. Run
`uv run bankeriq-runtime validate` for the complete local workflow, or select
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

The `codex/runtime-infrastructure` branch covers containerisation, persistent
state and durable execution, message passing, private Blob storage and report
delivery, and hosted runtime validation in Azure. Infrastructure remains managed
through Terraform. Production identity and input admission require integration;
the browser app and self-hosted specialist remain separate work.
