# Tests

```text
tests/
  unit/
    coordinator/   # Model/tool loop and execution budgets
    durable/       # Continuation matching, claims and resumption
    providers/     # Configuration, selection and provider-specific tests
      foundry/     # API translation and SDK calls through a local HTTP transport
    hosting/       # External request schemas and authority-field rejection
    policy/        # Output filtering and restricted receipts
    runs/          # Run-state invariants
    storage/       # Ownership, checkpoints and concurrent updates
    startup/       # Smoke-test assertions and configuration admission
    tools/         # Definitions, results, registry and gateway
  integration/     # Complete synthetic workflows and their shared harness
  architecture/    # Core/provider import boundaries and SDK-free startup
  tooling/         # Readability checks and Python formatting
```

`integration/test_startup.py` launches the executable in fresh processes, checks
its loopback HTTP boundary and completes two turns before graceful shutdown.
Terraform configuration tests live under `infra/azure/tests/` and use mocked
providers; run them with `terraform -chdir=infra/azure test`.

Place focused runtime tests under `unit/` using the owning component's folder.
Keep small fakes with the tests that use them. The integration harness and its
providers live together under `integration/`; see its [guide](integration/README.md)
for the workflows and validation limits.

Run all quality checks and tests:

```sh
uv run python scripts/verify.py
```

Run a particular group:

```sh
uv run python -m pytest tests/unit -q
uv run python -m pytest tests/unit/tools -q
uv run python -m pytest tests/integration -q
uv run python -m pytest tests/architecture -q
uv run python -m pytest tests/tooling -q
```
