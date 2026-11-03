# Agent runtime guidance

Read the root instructions, [README.md](README.md) and the affected component guide.

- This package implements harness interfaces using workers, persistence, sessions,
  provider/hosting adapters and telemetry. It may import `agent_harness`; it must not
  import `agent_platform`. Application composition supplies catalog, identity and policy.
- Keep queue admission/settlement, leases, generation fences, attempt budgets and
  unknown-effect recovery intact. External calls remain outside retried transactions.
- Keep pure memory selection and prompt compilation in the harness. Runtime memory
  coordinates source reads, provider calls and fenced publication using those decisions.
- Preserve applied SQL migration bytes and numbers. A package move requires no schema
  migration. Validate package data in the installed wheel, not only an editable checkout.
- Use real local PostgreSQL/Redis and independent worker processes for durable behaviour.
  Preserve the synchronous in-memory adapter and scoped output/delivery separation.
- Follow `storage/memory.py` for commenting. Never log credentials or message/tool bodies.
