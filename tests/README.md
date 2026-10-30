# Tests

```text
tests/
  unit/
    coordinator/   # Model/tool loop and execution budgets
    durable/       # Continuation matching, claims and resumption
    policy/        # Output filtering and restricted receipts
    runs/          # Run-state invariants
    storage/       # Ownership, checkpoints and concurrent updates
    tools/         # Definitions, results, registry and gateway
  integration/     # Complete synthetic workflows and their shared harness
  tooling/         # Readability checks and Python formatting
```

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
uv run python -m pytest tests/tooling -q
```
