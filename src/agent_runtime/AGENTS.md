# Agent runtime layout

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
- Temporary migration instruction: as the components are built, move run state
  into `runs/`, messages and conversation history into `conversations/`, and JSON
  snapshot helpers out of `types/` into an appropriate helper module. Update
  imports, tests and documentation, then remove this temporary instruction once
  all these moves are complete.
