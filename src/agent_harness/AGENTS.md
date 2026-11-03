# Agent harness guidance

- Read [README.md](README.md) and the root instructions before changing behaviour.
- This package owns model/tool decisions, pure context/compaction policy, prompt
  compilation, permission/output enforcement and interfaces. Runtime adapters implement
  these interfaces. Do not import `agent_runtime`, `agent_platform`, provider SDKs,
  database/cache clients or scheduling/network services here.
- Keep persisted record tags, prompt identities and content hashes stable during moves.
  The journal codec validates data without performing storage I/O.

- Prefer Python expressions that make intent explicit and are easy to read.
  Use descriptive methods when operator overloads obscure the meaning; for
  example, use `required_permissions.issubset(granted_permissions)` for a set
  inclusion check.
- Keep `types/` limited to shared, logic-free definitions. Place behaviour and
  validation with the component that owns them.
- Use concrete examples in comments to explain unfamiliar interfaces and
  consequential behaviour. For callables, show a small implementation, how it is
  passed, and representative input/output. For example, the `visible_filter`
  comment in `policy/output.py` shows `public_total(payload)` returning
  `{"total": payload["total"]}`, passed as `visible_filter=public_total`, turning
  `{"total": 42, "private_notes": "..."}` into `{"total": 42}`. Keep examples
  concise and aligned with the code's behaviour.
