# Provider boundaries and selection

Microsoft Foundry remains the current model and hosting implementation. The
runtime depends on provider-neutral interfaces. Application startup chooses
model access and hosting independently and owns their lifetimes.

```text
Application configuration
  ├─ MODEL_PROVIDER   -> model factory -> ModelClient
  │                                      ↕
  │                                  Coordinator -> tools / policy / state
  └─ HOSTING_PROVIDER -> host factory  -> RuntimeEndpoint
```

`providers/foundry/` owns Microsoft/OpenAI SDK imports, Azure credentials,
inference message translation, response parsing and the hosting wrapper.
`coordinator/`, `tools/`, `policy/`, `storage/`, `durable/` and the application
request handler import no provider implementations or vendor SDKs.

## Choose a provider

The application reads trusted startup configuration with
`agent_platform.configuration.load_settings(environment)`. Both provider
choices default to `foundry`:

```sh
MODEL_PROVIDER=foundry
MODEL_NAME=coordinator-deployment
HOSTING_PROVIDER=foundry
FOUNDRY_OPENAI_BASE_URL=https://RESOURCE.openai.azure.com/openai/v1/
```

Set `FOUNDRY_AUTH_MODE=azure_cli` to use an existing `az login` session, optionally
with `AZURE_TENANT_ID`. The signed-in principal needs inference access to the
Foundry account. The default `api_key` mode reads `AZURE_OPENAI_API_KEY` through
the application's secret configuration. Credentials are resolved only by the
selected Foundry factory. `MODEL_NAME` identifies the
configured model/deployment for the selected adapter. Optional common settings:

- `MODEL_INSTRUCTIONS`: explicit application-owned fixed-prompt override.
  Otherwise startup uses the pinned JSON runtime/system catalog.
  Shared deployments inject a published-prompt resolver into `open_runtime()`.
- `MODEL_MAX_OUTPUT_TOKENS`: positive output budget, default 4096.
- `MODEL_REASONING_EFFORT`: an explicit `ReasoningEffort` value; omission keeps the
  provider default.

Foundry-specific settings stay in its factory:

- `FOUNDRY_REASONING_EFFORTS`: comma-separated efforts verified for the deployment
  with function calling, for example `low,high`.
- `FOUNDRY_TIMEOUT_SECONDS`: finite positive request timeout, default 60.

Unsupported providers and unsupported explicit reasoning settings fail startup.
No automatic fallback sends requests to a different provider. The selected client
is retained across turns and approval/job continuations for this runtime instance.
HTTP request bodies cannot choose providers or replace configured tools.

## Application wiring

`agent_platform/composition.py` contains `open_runtime()`, the composition
point that opens the selected model, constructs the coordinator/continuation
handler, and creates the selected host. Use it with the application's dependencies:

```python
import os

from agent_platform.configuration import load_settings
from agent_platform.composition import open_runtime

settings = load_settings(os.environ)
with open_runtime(
    settings,
    environment=os.environ,
    store=store,
    registry=registry,
    gateway=gateway,
    permissions_for=access.permissions_for,
    continuation_resolver=resolver,
    authenticate=verify_application_identity,
    prepare_input=application_input_policy,
) as server:
    server.run()
```

The context manager closes the selected model's connections after the server
stops, including failures during startup or execution. Application-owned storage
and tool connections retain their own lifetimes. Identity, input admission and
durable providers still need production implementations.

## Add another provider

1. Implement `ModelClient.complete(ModelRequest) -> ModelReply` in
   `providers/<name>/`. Translate messages, tools, final-answer signals and errors
   there. The gateway continues to own authorization and output enforcement.
2. Supply a model factory that accepts `ModelSettings`, opens its SDK client,
   yields the adapter and closes the connection on exit.
3. Register that factory once in `agent_platform/providers.py`.
4. Select its registered name through `MODEL_PROVIDER`. Hosting can remain Foundry.

A new hosting integration supplies a separate factory accepting `RuntimeEndpoint`
and the authentication callback, returning a server with `run()`. Register it in
the hosting map and select it through `HOSTING_PROVIDER`.

The application can inject explicit factory maps into `open_runtime(models=...,
hosts=...)`; local tests use this to exercise two model providers with the same
Foundry host and policy stack. New real providers require their own API mapping
and compatibility tests. Only Foundry has a real provider implementation today.
Provider-specific reasoning/continuation requirements must be handled by each
adapter before enabling those features.

## Dependency isolation

Provider SDKs are optional package extras. Install both Foundry integrations for
a deployment with:

```sh
uv sync --locked --no-dev --extra foundry-model --extra foundry-hosting
```

`uv sync --locked --no-dev` installs the core without vendor SDKs. The development
group includes SDKs needed to test the implemented adapters, so the existing
`uv run python scripts/verify.py` command checks the complete repository.

Architecture tests reject provider imports from core components and verify that
core and startup modules load when vendor imports are blocked. An isolated local
installation was also checked with Azure and OpenAI SDKs absent. Integration tests
verify selection, cleanup, waits and output separation with synthetic providers.
The executable profile also passed live Foundry inference on 2 November 2026
using regional `gpt-4o` in Sweden Central; see the
[Azure validation evidence](../../../infra/azure/README.md#validation-evidence).
Azure-hosted execution and enterprise identity integration remain pending.
