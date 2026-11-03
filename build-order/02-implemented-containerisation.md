# Containerisation

Status: implemented and validated locally on Linux ARM64. Depends on initial runtime.

1. **Package the application** — Reuse the synthetic composition with locked
   dependencies, pinned base images and non-root execution.
2. **Configure container startup** — Supply an explicit bind address, required
   validation token, readiness check and direct process execution for shutdown signals.
3. **Validate the container** — Exercise the offline workflow, token/input checks,
   two HTTP turns and graceful shutdown with a read-only filesystem.

Evidence: the [Docker verifier](../scripts/verify_container.py) passed;
`uv run python scripts/verify.py` passed all 244 Python tests and static checks.
Other architectures and Azure execution remain unvalidated. See the
[container guide](../tests/validation/README.md#container-validation).
