# BankerIQ agent platform

A design and development scaffold for an Azure-hosted banking assistant. This is
an exploratory design inspired by the role discussion, not Rabobank's confirmed
architecture.

The coordinator uses Microsoft Foundry Agent Service. A separate specialist with
an internally hosted model handles sensitive customer data. Business tools enforce
user permissions; approved output returns to the coordinator, while sensitive
reports reach the user through an authenticated download.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and security boundaries.

## Current scope

This repository contains design documentation, an empty Python package and local
quality checks. No agent, API, Azure resources or model integration is implemented.
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

Define one synthetic-data workflow: authenticated request, authorised tool call,
internal specialist, policy-approved return, and protected report download.
Decide the Foundry agent type and durable workflow ownership before implementation.
