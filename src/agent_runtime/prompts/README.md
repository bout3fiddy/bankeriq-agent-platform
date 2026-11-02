# Shared system prompts

Implemented with local PostgreSQL and synthetic-model validation. Application
startup injects a `PromptResolver` into `open_runtime(prompts=...)` or directly
into `Coordinator`. Every new model step resolves its instructions before
inference. `ModelRequest` carries the composed text and exact manifest; Foundry
encodes that request's text as the system message without changing shared client
state. Conversation history retains only user/assistant messages and tool records.

## Composition and compatibility

`resolution.py` contains the provider-neutral types, deterministic composition and
fixed resolver. `PromptManifest` records application/environment, global version
and content hash, activation revision, runtime version/hash and composer version.
Shared composition puts versioned runtime instructions first, followed by global
guidance. Runtime instructions describe independent and repeated tool calls,
dependent-call ordering, restricted receipts and application-controlled permissions.
Actual tool dispatch remains synchronous in this phase.

Instruction text is authored in [versioned JSON compositions](../../../prompts/README.md).
Application startup loads the pinned runtime/system defaults; the core receives
immutable definitions and requires an explicit resolver. `FixedPromptResolver`
preserves explicit trusted startup text and the synthetic demo's JSON-loaded text.
`open_runtime()` uses an injected shared resolver, configured file resolver, or
explicit `ModelSettings.instructions`, in that order. `FoundryModelClient` receives
instructions through each `ModelRequest`; no provider adapter owns a binding.

A prepared step stores its manifest with the model-step budget reservation,
checkpoint, context cursor and synthetic next-work records in one transaction.
`StateStore.save_checkpoint(prompt_resolver=..., model_steps=previous + 1)` is the
preparation boundary. Preparation requires unresolved tool calls to be settled.
The PostgreSQL resolver reads the binding from its configured primary while that
transition is being prepared; the binding read determines the version. Activation
after that read affects later preparations. Only manifests enter checkpoints;
bodies remain in the immutable prompt store.

Replaying a preparation uses its original stable transition ID and recorded
manifest, even if the active binding changed after commit but before the caller
received acknowledgement. `store.load_model_step(context, step_number)` recovers
the original checkpoint, including context cursor and budget. Pass its manifest
to `resolver.instructions(manifest)` to reproduce the text. Current permission
checks still decide which tools/results can be used. Loading a prepared step
does not grant execution ownership or authorise another external effect; worker
claiming and retry orchestration remain a later group.

## Publication and activation API

Apply the existing numbered migrations with `migrate(writer_dsn)` during release
setup. Migration 004 adds version records, scoped bindings, publication/activation
audit records, withdrawals and prepared-step references. Existing checkpoint JSON
without a manifest remains readable; subsequent preparations add one. Migration
005 adds source provenance for JSON publications.

The management API is separate from chat requests and tool registration:

```python
from agent_runtime.prompts.resolution import PromptScope
from bankeriq_agent_platform.storage.prompts import (
    PostgresPromptResolver,
    PromptAdministration,
)

scope = PromptScope("bankeriq", "development")
management = PromptAdministration(
    admin_dsn,
    scope,
    authorize=can_manage_prompts,  # Server-owned policy: (verified Principal, scope) -> bool.
)
management.publish_definition(
    authenticated_admin,
    request_id="publish-guidance-v1",
    validation_reference="review-and-synthetic-evaluation-123",
)
_, expected_revision = management.active_binding(authenticated_admin)
revision = management.activate(
    authenticated_admin,
    request_id="activate-guidance-v1",
    version="bankeriq.system@1.0.0",
    expected_revision=expected_revision,
)
prompts = PostgresPromptResolver(prompt_reader_dsn, scope)
# Inject prompts=prompts into the existing open_runtime(...) composition.
```

Authenticate the administrator before constructing its `Principal`. The injected
authorizer checks every management call, including retries, against the exact
application/environment. It receives no model-supplied authority. Use a separate
management database identity; resolver SQL supports a SELECT-only identity. The
host's chat schemas do not expose publication, activation or withdrawal commands.
A production HTTP management transport and enterprise admin authentication remain
application deployment work; this release supplies the callable management API.

Publication validates nonblank UTF-8 text up to 32 KiB, rejects NUL, requires a
bounded validation reference and accepts only supported runtime/composer versions.
Content suitability and evaluation approval belong to the trusted authoring
workflow. A reference string does not prove model adherence or establish permission.
Published rows include author, timestamp and hashes; SQL triggers reject edits or
deletions of published versions, operations and withdrawals.

Activation checks an expected binding revision and appends an audit record in the
same transaction as the pointer update. To roll back, activate an earlier available
version with a new request ID and the current expected revision; the activation
revision increases. Two publishers using the same expected revision cannot both
win. Request IDs are scoped by application/environment and bind the actor and
operation fingerprint. Matching retries return the recorded outcome; conflicting
reuse fails. Unknown commit acknowledgements reconnect and resolve the original
identity on the primary, with at most three database attempts.

## Cache, failure and retention behavior

`PostgresPromptResolver` checks the active binding on every resolution. It caches
at most 64 immutable bodies by version/hash within its fixed application/environment
scope. Composition is deterministic and is recomputed; no composed-context or
active-pointer cache exists. An eventual composed-context cache must key by the
complete manifest. No invalidation message is required for correctness; Redis
caching belongs to the next group.

`management.withdraw(admin, request_id=..., version=...)` permanently marks a
version unavailable while retaining its body/audit history. Both fresh resolution
and cached old-step recovery check withdrawals on the primary. Missing bindings,
withdrawn versions, unavailable database access and incompatible runtime/composer
versions raise `PromptUnavailableError` before inference. Shared mode never falls
back to startup text. Availability is checked at resolution/reconstruction;
already dispatched model calls retain their supplied instructions. Emergency
interruption still requires run cancellation.

Keep old runtime/composer implementations available while recoverable manifests
reference them. The current release supports JSON-defined `bankeriq.runtime@1.0.0` /
`runtime-system-v1` and the fixed-profile format; incompatible versions fail explicitly. Keep prompt
versions, activation records and prepared-step references for the full run/audit/
replay retention window. Physical prompt deletion is disabled. A database restore
must follow the storage recovery-generation/fencing procedure; both administration
and resolvers pin that generation. Production retention and backup restore remain
operational work.

## Validation

Use the disposable container setup in the
[storage guide](../../bankeriq_agent_platform/storage/README.md), then run:

```sh
uv run python -m pytest tests/integration/test_prompts.py tests/unit/prompts \
  tests/unit/providers/foundry/test_model.py -q
uv run python scripts/verify.py
```

Set `BANKERIQ_TEST_POSTGRES_DSN` for real PostgreSQL coverage; database tests skip
explicitly when it is absent. The synthetic suite covers active/idle/new turns,
two resolver instances, publication during inference, resumed tool/approval waits,
concurrent activation, rollback, lost acknowledgements after a newer activation,
old-step recovery in a fresh process, write-boundary rollback, missing/withdrawn
versions, read-only database access and denied administration. Concurrent calls
through one Foundry SDK client retain separate instructions. Adversarial global
text does not expand tool discovery or bypass gateway/output enforcement.

Executed on 3 November 2026 against local PostgreSQL 17.11: all 292 tests and
static checks passed with the database enabled, including JSON source provenance,
JSON prompt recovery, file validation and the existing shared-prompt scenarios. The
packaged container passed its offline and HTTP verification.

This evidence checks composition, storage and authorization behavior with scripted
models and local HTTP transports. Live model adherence, production admin identity,
Azure database roles/networking, Redis, business overlays, queued worker recovery
and parallel tool execution remain unvalidated or deferred.
