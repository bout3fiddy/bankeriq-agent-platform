# Synthetic validation harness

This repository-only application exercises the coordinator, tools, permissions,
HTTP handlers, continuation and output isolation with synthetic data. It lives
under `tests/validation/` and is excluded from the application wheel. Reusable
server/startup wiring remains in `src/bankeriq_agent_platform/`.

Both scripted and live-provider runs use the normal BankerIQ runtime/system
[prompt catalog](../../prompts/README.md). There is no test-specific system prompt
or prescribed tool sequence in model instructions. `MODEL_INSTRUCTIONS` overrides
are rejected here. The ordinary user request lives in
[fixtures/request.json](fixtures/request.json).
Expected outcomes and scoring stay in the test harness, outside model context.

## Local checks

```sh
uv sync --locked
uv run python -m tests.validation validate
mkdir -p scratch/transcripts
uv run python -m tests.validation validate --transcript scratch/transcripts/mock.md
```

The default mock is scripted: saved permitted results determine its next action.
It checks runtime plumbing and cannot demonstrate model planning or adherence.
The workflow loads values, calculates 42, creates a restricted report and resumes
its completion receipt. Checks require the three tool calls in dependency order,
one continuation, the expected permitted outputs and no restricted data in history.

Transcripts contain the actual resolved instructions, permitted conversation
fields, execution trace and model label. No private reports, credentials or artifact
locations are exported. Output-isolation checks run before export; other workflow
failures preserve safe observed history. Existing transcript files are never overwritten.

```sh
BANKERIQ_VALIDATION_TOKEN=synthetic-local-token uv run python -m tests.validation serve
```

The listener defaults to loopback port 8088. It accepts only the request fixture,
with test authentication mapping the bearer token to a fixed synthetic identity.
This input allowlist prevents arbitrary customer data entering the test application;
it limits what this single scenario establishes. Reusable HTTP request shapes are
in the [hosting guide](../../src/agent_runtime/hosting/README.md).

## Container validation

```sh
docker build -t bankeriq-runtime:synthetic .
uv run python -m scripts.verify_container
```

The Dockerfile builds a synthetic validation image with the `foundry-hosting` and
`validation` extras. It explicitly copies `tests/validation/` and starts
`python -m tests.validation`; the application wheel contains no synthetic harness.
Only the selected fixture directory enters the image, with local secrets, configs,
Terraform state and transcripts excluded by `.dockerignore`.

The verifier checks the offline workflow, missing token, readiness, non-root HTTP,
two turns and shutdown. Manual startup requires `BANKERIQ_VALIDATION_TOKEN`:

```sh
export BANKERIQ_VALIDATION_TOKEN=synthetic-local-token
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --publish 127.0.0.1:8088:8088 --env BANKERIQ_VALIDATION_TOKEN bankeriq-runtime:synthetic
```

## Live-provider validation

Configure the existing Foundry endpoint using the [Azure guide](../../infra/azure/README.md).
Then explicitly run:

```sh
uv run python -m tests.validation validate --provider foundry \
  --config infra/azure/runtime.json --transcript scratch/transcripts/live.md
```

This invokes the real provider with the real prompt catalog and synthetic tools.
Tool schemas and synthetic data remain visible to the model; the fixture is narrow
and does not establish broad banking reliability or resistance to evaluation bias.
Prompt identity, permitted tool execution and isolation are checked locally. Live
model behavior after removing the demo instructions has not yet been evaluated.
Historical Foundry evidence used the old prescribed instructions and remains labelled
as such in the Azure guide. This cleanup does not provision cloud resources.

Executed on 3 November 2026: `uv run python scripts/verify.py` passed all 301
tests with local PostgreSQL 17.11 enabled, plus static checks. The rebuilt synthetic
container passed offline workflow, token, readiness, two-turn HTTP and shutdown
checks. An isolated wheel loaded the application catalog and reusable server with
no validation fixtures or synthetic CLI packaged. No live-model calls were made.
