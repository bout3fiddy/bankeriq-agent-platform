# Shared system prompts

Status: complete and locally validated. Depends on PostgreSQL journal and job records.

1. **Separate instructions from client lifetime** — Add explicit resolved
   instructions and a prompt manifest to the provider-neutral model request. Use the
   runtime/system catalog for synthetic validation too. Adapters encode
   request-scoped instructions without mutating shared model clients; history keeps
   its existing user/assistant/tool roles.
2. **Store and publish global versions** — Add immutable prompt bodies/hashes,
   compatibility/validation metadata and an application/environment active binding.
   Separate admin publication from chat/tool access. Validate drafts and activate
   with an expected binding revision; rollback creates a new activation record.
3. **Resolve at each new model step** — Read the binding during step preparation
   and persist the manifest before inference. All sessions inherit the current
   global version; in-flight steps and retries retain their resolved version.
   New steps after tool/approval waits pick up updates. Keep compatible runtime
   instructions available for recovery; unavailable versions block explicitly.
4. **Compose and cache safely** — Combine runtime and global guidance in fixed
   order, including parallel/repeated-tool guidance. Cache immutable bodies by
   version; the active binding remains a PostgreSQL read. Include the manifest in
   composed-context keys. Add Redis body caching in the Redis group; local immutable
   caching suffices here. Business overlays remain deferred.
5. **Validate propagation and recovery** — Test existing/new/idle sessions, two
   workers, publication during inference, resumption after tool waits, old-step
   retries, concurrent publishers, rollback and lost invalidation signals. Verify
   unauthorised publication is denied, client instructions cannot race between
   sessions, and prompt text never changes trusted tool permissions.

Completed: one activation affects every newly resolved model step without
restarting sessions. Prepared steps retain their exact manifest and recoverable
instructions. The separate management API supports immutable publication,
revision-checked activation/rollback and withdrawal. The
[prompt guide](../src/agent_runtime/prompts/README.md) documents composition,
authorization, retention, reproduction commands and validation limits.

Evidence: `uv run python scripts/verify.py` passed all 275 tests and static checks
with local PostgreSQL 17.11 enabled. Fifteen shared-prompt PostgreSQL scenarios
cover propagation, prepared-step recovery, concurrent activation, rollback and
lost commit acknowledgements. Local HTTP tests
verify concurrent request-scoped instructions through one Foundry client. Tool
permissions and restricted-output boundaries remain enforced by application code.
Live model behavior and production admin authentication remain later work.
Redis immutable-body caching was subsequently added in the session group.

JSON authoring follow-up completed on 3 November 2026: [prompt definitions](../prompts/README.md)
now separate runtime/system roots, security rules and guidelines into versioned
JSON compositions. Synthetic validation fixtures subsequently moved under tests/validation/.
Catalogs pin exact component versions and
file hashes. Migration 005 preserves publication source provenance. The pre-JSON
runtime-v1 compatibility files and composer have subsequently been removed. Application startup
performs file parsing;
core receives immutable prompt values. Offline authoring commands validate, render
and register new versions. Wheels and containers package the assets.

Follow-up evidence: all 292 tests and static checks passed with local PostgreSQL
17.11 enabled. The rebuilt synthetic container passed offline workflow, token,
readiness, non-root HTTP, two-turn and shutdown checks; packaged prompt loading
also passed without repository files. Live-model adherence to the new wording
has not been evaluated.

Validation harness cleanup: synthetic tools, model, test authentication, CLI and
transcript/scoring helpers now live under `tests/validation/`. The application
retains reusable `LocalServer` and `open_runtime()` wiring. The test-only system
prompt was removed; both mock and live-provider validation use the real BankerIQ
runtime/system definitions. The synthetic Docker image explicitly includes this
harness; the application wheel excludes it. Earlier live evidence used prescribed
instructions and does not validate the current prompt composition.

Executed on 3 November 2026: `uv run python scripts/verify.py` passed all 301
tests with local PostgreSQL 17.11 enabled, plus static checks. The rebuilt synthetic
container passed offline workflow, token, readiness, two-turn HTTP and shutdown
checks. An isolated wheel loaded the application catalog and reusable server with
no validation fixtures or synthetic CLI packaged. No live-model calls were made.
