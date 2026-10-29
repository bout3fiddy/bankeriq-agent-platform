# Code style

- Use descriptive variable and parameter names; expand opaque abbreviations.
- Separate logical stages with blank lines, including after function docstrings.
- Keep public entry points before helpers and describe their inputs and outputs.
- Explain what meaningful blocks do and why; avoid narrating assignments.
- Keep side effects and credentials behind explicit interfaces.
- Use typed inputs and return values, with explicit failure behaviour.

Python tooling targets 3.14 and a 100-column limit. Ruff checks E/F/I/UP/B/SIM
rules and formatting; Basedpyright checks types. The AST readability checker
enforces paragraph spacing and rejects future annotations. This tooling is
adapted from the local MONKI GPU project, without its native/scientific checks.

```sh
uv run ruff format src scripts tests
uv run python scripts/verify.py
```

Keep core runtime code under `src/agent_runtime/`, platform code under
`src/bankeriq_agent_platform/`, checks under `scripts/`,
and behavioural tests under `tests/`. Add cloud dependencies only when needed
for an implemented component.
