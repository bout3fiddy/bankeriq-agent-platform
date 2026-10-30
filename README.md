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

## Current scope

This repository contains design documentation, core runtime data types, a
permission-filtered tool registry, completed-output enforcement, a synchronous
tool gateway, conversation/run state, an in-memory store with atomic checkpoint
updates, a bounded coordinator loop and a `src/agent_runtime/durable/` scaffold
for mocked job and approval events. Scripted model replies exercise repeated
tool calls and resumption. The [integration harness](tests/integration/README.md)
connects these components through synthetic calculation, approval, report and
delivery workflows, including failure and permission changes.
Local quality checks include behavioural tests. External APIs, Azure resources
and live model integration remain unimplemented.
Python 3.14 is the local tooling baseline copied from MONKI; deployment runtime
compatibility must be checked before selecting an Azure hosting target.

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

Implement the Foundry model adapter behind the existing model interface after
checking the current SDK and response formats. Raw chat-input policy, production
delivery, Hosted Agents integration and durable recovery remain unresolved work.
