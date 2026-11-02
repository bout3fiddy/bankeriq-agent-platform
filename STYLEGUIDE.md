# Code style

- Use descriptive variable and parameter names; expand opaque abbreviations.
- Separate logical stages with blank lines, including after function docstrings.
- Put a blank line before each standalone comment block. Consecutive comment-only
  lines form one block. File-start comments and inline comments are exempt.
- Keep public entry points before helpers and describe their inputs and outputs.
- Explain what meaningful blocks do and why; avoid narrating assignments.
- Keep side effects and credentials behind explicit interfaces.
- Use typed inputs and return values, with explicit failure behaviour.

Python tooling targets 3.14 and a 100-column limit. Ruff checks E/F/I/UP/B/SIM
rules and formatting; Basedpyright checks types. The readability checker uses
AST and token checks to enforce paragraph and comment spacing and reject future
annotations. This tooling is adapted from the local MONKI GPU project, without
its native/scientific checks.

The project formatter runs Ruff and then inserts the required comment spacing.
Verification and pre-commit check this combined format, including comment blocks
immediately after loop headers or inside parentheses.

```sh
uv run python -m scripts.format_python
uv run python scripts/verify.py
```

Keep core runtime code under `src/agent_runtime/`, platform code under
`src/bankeriq_agent_platform/`, checks under `scripts/`,
and behavioural tests under `tests/`. Group focused runtime tests under
`tests/unit/<component>/`, complete workflows under `tests/integration/`, and
development-tool checks under `tests/tooling/`. See [tests/README.md](tests/README.md).
Add cloud dependencies only when needed for an implemented component.
