# Agent runtime

Reusable execution and I/O adapters for the [agent harness](../agent_harness/README.md).
The [platform backend](../agent_platform/README.md) supplies configuration, identity,
application prompts and tool bindings. Runtime code has no platform-package dependency.

- [Workers](workers/README.md): PostgreSQL queues, leases, retry/recovery controls,
  bounded execution, cancellation and one durable continuation after tool fan-out.
- [Storage](storage/README.md): PostgreSQL transactions, immutable migrations, history
  materialisation and prompt publication. `storage/memory.py` implements the same harness
  `StateStore` interface for synchronous local execution.
- [Sessions](sessions/README.md): disposable Redis views, activity retention and bounded
  progress/SSE recovery. PostgreSQL remains authoritative for accepted work and final output.
- [Memory](memory/README.md): durable source reads, summary reservation/publication and
  bounded retrieval. Pure assembly/selection lives in `agent_harness.memory`.
- [Providers](providers/README.md): model/hosting SDK adapters, credentials and transport.
- [Hosting](hosting/README.md): transport-neutral endpoint/session access interfaces and
  the existing synchronous hosting boundary. Application HTTP routes live in the platform.
- `prompts/files.py`: bounded file loading from an explicit directory; the platform selects
  its assets and passes the resulting catalog into PostgreSQL prompt services.
- `telemetry.py`: allowlisted lifecycle/measurement events without message or tool payloads.

Provider calls happen outside retried database transactions. Settlement preserves ownership,
budget, prompt-manifest and recovery-generation checks. Restricted output and user delivery
metadata remain separate. The [transaction design](../../ARCHITECTURE.md) describes the fences.

For local verification, configure the disposable PostgreSQL/Redis services documented in
[workers/README.md](workers/README.md), then run `uv run python scripts/verify.py` from the
repository root. Tests use synthetic models/tools and independent worker processes.
The hosted pilot entry point remains `python -m tests.azure.pilot`; it is a validation fixture.
