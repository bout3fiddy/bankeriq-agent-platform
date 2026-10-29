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
permission-filtered tool registry and local
quality checks with behavioural tests. No agent loop, API, Azure resources or
model integration is implemented.
Python 3.14 is the local tooling baseline copied from MONKI; deployment runtime
compatibility must be checked before selecting an Azure hosting target.

## Development

Install [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync --locked
uv run python scripts/verify.py
uv run pre-commit install
```

The verification command checks the lockfile, formatting, lint, paragraph spacing
and types. It runs pytest when test files exist and explicitly reports when none
exist. Pre-commit runs static checks. See [STYLEGUIDE.md](STYLEGUIDE.md).

## Next step

Define a synthetic-data workflow covering permission-based discovery, authorised
execution, visible and restricted outputs, and protected report delivery. Resolve
raw chat-input policy and validate Hosted Agents and durable workflow ownership
before implementation.
