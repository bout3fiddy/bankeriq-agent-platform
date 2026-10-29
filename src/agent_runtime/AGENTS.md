# Agent runtime layout

- Prefer Python expressions that make intent explicit and are easy to read.
  Use descriptive methods when operator overloads obscure the meaning; for
  example, use `required_permissions.issubset(granted_permissions)` for a set
  inclusion check.

- Keep `types/` limited to shared, logic-free definitions. Place behaviour and
  validation with the component that owns them.
- Temporary migration instruction: as the components are built, move run state
  into `runs/`, messages and conversation history into `conversations/`, and JSON
  snapshot helpers out of `types/` into an appropriate helper module. Update
  imports, tests and documentation, then remove this temporary instruction once
  all these moves are complete.
