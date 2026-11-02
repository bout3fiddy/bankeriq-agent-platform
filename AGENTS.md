# Development expectations

## Communication

- Keep responses concise and state facts directly.
- Avoid filler, process narration and rhetorical comparisons.
- Distinguish proposed designs, implemented behaviour and executed evidence.
- Explain consequential decisions through concrete inputs, outputs and trade-offs.

## Implementation

- Read README.md for current scope, ARCHITECTURE.md for the proposed design,
  components.md for logical component responsibilities, and STYLEGUIDE.md before
  changing code.
- Prefer focused, incremental changes and justify additional abstractions.
- Use descriptive names and blank lines between logical stages.
- Add what-and-why comments around meaningful behaviour and non-obvious decisions.
- Keep parsing, credentials, network access and scheduling separate from core logic.
- Pass explicit inputs and dependencies; avoid catch-all mutable state.
- Avoid repetitive defensive checks in internal typed code. Validate external
  inputs at boundaries and retain checks for meaningful domain invariants.
- Fail explicitly for unsupported options and invalid inputs.
- Preserve unrelated work, supplied fixtures and reference outputs.
- Keep documentation aligned with implemented behaviour and label unfinished work.

## Validation

- Use uv for Python commands and dependency changes.
- Keep Python version targets aligned across package metadata and tooling.
- Run `uv run python scripts/verify.py` after relevant changes.
- Write tests for meaningful behaviour and invariants; avoid tests that only mirror
  the implementation.
- Use synthetic data and local fakes for routine tests.
- Report what was actually checked and any validation gaps.
- Make performance, security and reliability claims only to the extent supported
  by evidence.

## Changes and external actions

- Keep secrets and private data out of source, fixtures, logs and commits.
- Ask before destructive changes or external actions not already authorised.
- Do not commit, push, publish or deploy unless requested.

## Infrastructure

- Create cloud resources strictly through Terraform provider-managed resources.
  Keep Azure configuration under `infra/azure/` and read its
  [resource inventory](infra/azure/README.md) before infrastructure work.
- This includes resource groups, model deployments, hosted-agent definitions,
  identities, role assignments and Terraform state storage. Do not create them
  through portals, Azure CLI, SDK scripts, ARM/Bicep or Terraform provisioners
  that execute creation commands. Manage subsequent changes through Terraform.
- CLI and SDK use for authentication, inspection, artifact publishing and runtime
  calls is allowed within the authorised task. Review a Terraform plan before
  applying an authorised infrastructure change.
