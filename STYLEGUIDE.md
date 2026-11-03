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

Keep agent behaviour and policy under `src/agent_harness/`, execution/adapters under
`src/agent_runtime/`, backend application code under `src/agent_platform/`, and the
browser application under `frontend/`. Keep checks under `scripts/` and behavioural
tests under `tests/`. Group focused runtime tests under
`tests/unit/<component>/`, complete workflows under `tests/integration/`, and
development-tool checks under `tests/tooling/`. See [tests/README.md](tests/README.md).
Add cloud dependencies only when needed for an implemented component.

JSON files must be valid JSON with no source line longer than 100 characters,
including indentation. Verification and pre-commit enforce this across tracked
and new unignored JSON files. Run `uv run python -m scripts.linting.check_json`.
For long prompt paragraphs, use nested arrays of source lines; the loader joins
these with one space and preserves blank lines between paragraphs.

The same 100-character maximum applies to all authored text files, including
Python, Terraform, Dockerfiles, YAML, TOML and Markdown. Verification and the
always-run pre-commit hook execute `uv run python -m scripts.linting.check_line_length`.
Git-tracked and new unignored files are checked, plus the local ignored
`infra/azure/runtime.json` export when present. Binary files and ignored caches,
build outputs and local secrets are outside the authored-source inventory.

Exceptions are limited to generated dependency locks (`uv.lock` and
`infra/azure/.terraform.lock.hcl`) and standalone Markdown URL destinations,
which cannot be split without changing their meaning. Put long links in reference
definitions with the URL on its own line; link labels and prose still obey 100.
Reflow wide tables as labelled lists. Never shorten hashes or change values just
to meet the line limit. Pretty-print local runtime JSON with `python -m json.tool`;
the Azure export example includes this step.
