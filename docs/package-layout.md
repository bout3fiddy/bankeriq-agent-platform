# Harness, runtime and platform package refactor

Recorded on 4 November 2026. Baseline: `7aa0d90e74eaca66853067d253a5a3199963371b`.
Work was isolated on `codex/harness-runtime-platform-refactor` before merging into
`codex/concurrent-sessions-streaming-and-memory-quality`. Group 12 remains planned.

## Package boundaries

- [Harness](../src/agent_harness/README.md): agent decisions, tool/permission/output policy,
  source-aware memory selection, prompt compilation, state interfaces and journal encoding.
- [Runtime](../src/agent_runtime/README.md): workers, recovery, persistence, Redis sessions,
  provider/hosting adapters, source-file loading and telemetry.
- [Platform](../src/agent_platform/README.md): backend API, identity, application composition,
  configuration, prompt assets and application-specific tool bindings.
- [Frontend](../frontend/README.md): documented location for the future browser application.
  No browser UI, new authentication flow or streaming feature is introduced by this refactor.

The runtime depends on harness interfaces. Application startup supplies the runtime's prompt
catalog explicitly. The harness has no runtime/platform or SDK/client dependency, and runtime
code has no platform import. Two package-boundary tests enforce these import directions.

The distribution is now `agent-platform`. Import paths, wheel package data, test helpers,
operator scripts, documentation and AGENTS guidance use the new layout. `composition.py`
retains `open_runtime()` and `compose_queued_runtime()`; HTTP routes and function behaviour
remain compatible. Source-level callers must use the new imports and supply `catalog` when
constructing PostgreSQL prompt services. No old-package import aliases are installed.

## State and deployment compatibility

All 15 applied SQL migrations and 12 pinned prompt JSON assets were compared against the
baseline and remained byte-for-byte identical. External locked dependencies are unchanged.
Persisted record tags, prompt IDs/content, memory profiles, queue operation/version names,
environment variables, tenant identifiers and cloud resource names remain unchanged.

A separate local database exercised compatibility between independent Python processes:

1. Baseline checkout applied migrations and created a synthetic conversation.
2. Its existing `tests.integration.async_process.composition()` admitted a run and completed
   `model_prepare`, leaving an inference job with its saved checkpoint/profile/manifest.
3. The refactored checkout loaded exactly that checkpoint, verified identical memory/profile
   and application prompt-manifest values, and ran inference plus three calculation children.
4. It committed exactly one joined group, resumed inference and completed the run with two
   model steps and three tool calls. The baseline checkout read the completed result again.

No new migration or persisted-profile rebuild was needed. This local compatibility exercise
does not establish zero-downtime hosted rollout or mixed-version production load guarantees.
The existing Azure deployment was left unchanged. Container-side diagnostics must use source
matching its deployed image; the current repository imports take effect on a future deployment.

## Executed validation

Python 3.14.8, PostgreSQL 17.11 and Redis 7.4.12 were used with synthetic models/tools.
The full quality gate passed **576 tests in 108.61 seconds, zero skips**, including independent
workers, recovery, concurrent tools, memory, permissions and restricted-output isolation.
The focused prompt/memory suite passed 111 tests before the full gate. No live model calls
or Azure changes were needed for this structural refactor.

Recreate disposable local services and run the full gate:

```sh
docker run -d --name agent-refactor-pg -e POSTGRES_PASSWORD=synthetic \
  -p 127.0.0.1:55449:5432 postgres:17
docker run -d --name agent-refactor-redis -p 127.0.0.1:56399:6379 redis:7
BANKERIQ_TEST_POSTGRES_DSN=postgresql://postgres:synthetic@127.0.0.1:55449/postgres \
BANKERIQ_TEST_REDIS_URL=redis://127.0.0.1:56399/0 \
  uv run python scripts/verify.py
```

Wait for PostgreSQL/Redis readiness before running tests. Reuse these names/ports only when
they are available; never replace another task's containers. After validation, remove only
the disposable containers created for this task.

Wheel and synthetic container checks:

```sh
uv build --wheel
docker build -t agent-runtime:package-refactor .
uv run python -m scripts.verify_container --image agent-runtime:package-refactor
```

The wheel contains all three packages, 15 SQL migrations, three Redis Lua scripts and 12
prompt JSON assets. It excludes test fixtures and the former platform package. The installed
synthetic image passed an offline workflow, required-token rejection, readiness, non-root
HTTP service, two conversation turns and graceful shutdown. No image was published.

The hosted Linux AMD64 image also built locally. An offline, read-only container imported
the pilot and loaded its prompt catalog, tokenizer and all 15 migrations successfully:

```sh
docker build --platform linux/amd64 -f infra/azure/Dockerfile \
  -t agent-runtime:package-refactor-hosted .
docker run --rm -i --platform linux/amd64 --network none --read-only \
  --entrypoint python agent-runtime:package-refactor-hosted - <<'PY'
import tests.azure.pilot
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.providers.foundry.tokens import FoundryTokenCounter
from importlib.resources import files

print(load_catalog().runtime().version)
print(FoundryTokenCounter("gpt-4o").encoding.name)
print(len(list(files("agent_runtime.storage").joinpath("migrations").iterdir())))
PY
```

This verifies image packaging; hosted traffic and deployment were outside this task.

All local documentation targets/anchors, including the available local roadmap/evidence,
were checked. `git diff --check` and repository static checks passed. Raw validation output
and the cross-checkout helper remain local in ignored scratch files; historical measurements
and source/image hashes retain their original meaning.

## Local roadmap preservation

The entire `build-order/` directory is gitignored and removed from the Git index. Its files
remain local in both checkouts, restored after Git applies tracked deletions during the merge.
Older evidence missing locally was recovered from its last tracked
revision without re-adding it. Fresh clones require the roadmap separately; tracked guides
and performance records remain available without it. Git history is not rewritten.
