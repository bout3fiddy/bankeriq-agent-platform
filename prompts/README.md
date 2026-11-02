# Versioned prompt definitions

All maintained instruction text lives in JSON. `catalog.json` selects exact
runtime and system roots and pins each component's file bytes with SHA-256.
`runtime/` defines orchestration/tool protocol; `system/` defines the BankerIQ
assistant role; `rules/` contains authority, data-boundary and action guidance;
`guidelines/` contains evidence and response guidance. `demo/` preserves the
synthetic scenario.

The system prompt describes the exploratory BankerIQ agent-runtime project,
evidence handling, financial uncertainty, tenant separation, prompt injection,
restricted outputs, secrets, approvals and consequential actions. These are model
instructions. Trusted application permission, gateway, approval and delivery
checks remain executable code and cannot be relaxed by editing a prompt.
Never put credentials, customer records or confidential tool payloads in these files.

## Schema and composition

The checked-in `schemas/*.schema.v1.json` files describe the validated format.
Every document has a `prompt` object containing `schema_version`, `id`, semantic
`version`, `kind`, `title`, `description`, `owner` and `change_summary`.
Fragments contain ordered `paragraphs`; a paragraph can be a string or an array
of source lines joined with one space. Use these arrays to keep JSON lines within
100 characters, including indentation. Separate paragraphs retain blank lines.
Sections and system/runtime roots contain
ordered `includes` references. For example:

```json
{
  "prompt": {
    "schema_version": 1,
    "id": "guidelines.example",
    "version": "1.0.0",
    "kind": "fragment",
    "title": "Example guidance",
    "description": "Synthetic authoring example",
    "owner": "BankerIQ runtime maintainers",
    "change_summary": "Initial version",
    "paragraphs": ["State the evidence supporting each result."]
  }
}
```

A composition includes it with `{"id":"guidelines.example","version":"1.0.0"}`.
Nested sections are supported. Each component appears at most once within a root;
cycles and duplicate inclusion fail. Rendering preserves listed order, adds titles
and joins sections with blank lines. Runtime precedes system instructions in one
request-scoped system message, outside conversation history. `renderer` versions
control document formatting; `composer_version` controls runtime/system layering.
Runtime roots also declare a unique `runtime_version` persisted in step manifests.

Schema version changes describe file-format compatibility. Component versions
identify immutable text/composition: patch for wording fixes, minor for compatible
additions, major for changed behavior. All changes require a new version, including
heading, metadata and whitespace changes because hashes cover exact file bytes.
Changing a child requires new parent versions up to the root. A runtime change
also requires a new `runtime_version`, conventionally `bankeriq.runtime@X.Y.Z`.
There are no floating versions, templates, executable includes, URL fetches or
environment interpolation. Unknown fields and duplicate JSON keys fail explicitly.
Files are bounded to 128 KiB; catalogs to 256 documents; each composition to 64
components, 12 nested edges and 32 KiB of rendered UTF-8 text.

## Editing and validation

1. Copy a component into its new version path, update `version`, `change_summary`
   and content. Keep existing published files unchanged.
2. Register the new file, dependencies first:
   `uv run python -m bankeriq_agent_platform.prompt_files add guidelines/example/1.0.0.json`
3. Create/register new parent compositions referencing the exact new versions.
   Update `catalog.json` defaults to the desired registered roots.
4. Validate and inspect the complete output:

```sh
uv run python -m bankeriq_agent_platform.prompt_files validate
uv run python -m bankeriq_agent_platform.prompt_files render
uv run python scripts/verify.py
```

`add` refuses to overwrite an existing identity/path or accept a broken graph.
It validates before atomically replacing the catalog. Use one authoring process
at a time and review the resulting Git diff. All commands accept `--directory`.
Runtime file access remains in application startup; core receives immutable text
and manifests. Wheels and containers include these assets. Trusted startup can
set `BANKERIQ_PROMPT_DIRECTORY` to an alternative reviewed directory. A running
local profile retains its snapshot until restart. Local prepared steps require
the same default manifest on restart; changed defaults fail closed for old steps.
Use shared publication for live updates and durable recovery across activations.
`MODEL_INSTRUCTIONS` remains an explicit trusted compatibility override.

## Shared publication and recovery

Construct `PromptAdministration(..., catalog=load_catalog(path))` and call
`publish_definition(admin, request_id="publish-system-1", validation_reference="review-123")`.
This publishes the default system as `bankeriq.system@1.0.0` and records both
roots' ordered component identities, file hashes, renderers and rendered hashes.
An optional `reference=Reference(id=..., version=...)` selects another system root.
Migration 005 adds bounded source provenance to immutable published rows.
Activate explicitly with the current binding revision using the
[management API](../src/agent_runtime/prompts/README.md). File edits alone never
change a PostgreSQL binding. Raw `publish(body=...)` remains a trusted compatibility
API; use `publish_definition` for the maintained JSON authoring workflow.

Every reader must have the referenced runtime versions in its catalog before
activation. Publish a new system version when changing its runtime pairing.
Retain old runtime files and composers for the full recovery/audit window;
shared system bodies are retained in PostgreSQL. Missing runtime versions or
hash mismatches block inference. The pre-JSON `runtime-v1` format has been removed;
saved steps referencing it cannot resume. Runtime policy updates require distributing assets to
readers, followed by explicit publication/activation.

## Reproducible local PostgreSQL checks

```sh
docker run --detach --name bankeriq-json-prompts-test \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_PASSWORD=synthetic --env POSTGRES_DB=bankeriq_test postgres:17
docker exec bankeriq-json-prompts-test pg_isready -U postgres -d bankeriq_test
BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55432/bankeriq_test \
  uv run python scripts/verify.py
docker rm --force bankeriq-json-prompts-test
```

Use a disposable database; the suite applies migrations and creates synthetic
records/roles. Tests cover catalog validation, deterministic composition, immutable
snapshots, source provenance, JSON prompt recovery and the existing shared-prompt
concurrency/rollback/restart/permission scenarios. Scripted model tests establish
application behavior; live model adherence to the new prose remains unvalidated.

Executed on 3 November 2026: the full gate passed 292 tests with PostgreSQL 17.11
and all static checks. `uv build` produced an sdist and wheel; the rebuilt container
passed `uv run python scripts/verify_container.py`, and its installed package loaded
`bankeriq.system@1.0.0` with networking disabled and no repository files.
