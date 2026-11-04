# Agent platform backend

Application composition for the internal banking assistant. This package wires the
[harness](../agent_harness/README.md) and [runtime](../agent_runtime/README.md) to configured
identity, input policy, prompts and tool bindings. The browser client belongs in
[`frontend/`](../../frontend/README.md).

- `composition.py`: `open_runtime()` preserves synchronous in-memory execution;
  `compose_queued_runtime()` wires the durable asynchronous coordinator. Worker startup
  and client lifetimes remain explicit application responsibilities.
- `api/queued.py`: authenticated admission, history, activity and SSE routes. The API
  returns after durable acceptance; model/tool execution runs through workers.
  Admission can reduce its pinned worker-attempt allowance with `worker_budget` (1–100);
  omitting it preserves the 100-attempt default. Duplicate requests cannot alter that allowance.
- `client/`: bounded CLI streaming, stale-attempt rejection and durable reconnect for
  the existing operator login. See [concurrent sessions](../../docs/concurrent-sessions.md).
- `identity/entra.py`: delegated Entra token checks and current synthetic/operator input
  policies. Workforce onboarding remains planned; operator authentication is implemented.
- `azure/connections.py`: application-managed Azure credentials and connection setup.
- `providers.py` and `configuration.py`: approved provider registrations and startup choices.
- `prompt_files/`: application asset selection and the offline authoring CLI. Generic
  compilation/schema lives in the harness; file I/O lives in runtime adapters.
- `server.py`: reusable synchronous local server wiring. Test tools/startup remain in
  `tests/validation/` and `tests/azure/`, outside the application wheel.

Prompt administration/resolution receives an explicit application catalog:

```python
from agent_harness.prompts.resolution import PromptScope
from agent_platform.prompt_files.loader import load_catalog
from agent_runtime.storage.prompts import PostgresPromptResolver

resolver = PostgresPromptResolver(
    dsn, PromptScope("bankeriq", "development"), catalog=load_catalog()
)
```

The example's deployment scope is an existing persisted identity. Package renaming preserves
that value, prompt IDs/hashes, environment-variable names, HTTP routes and database formats.

Run `uv run python -m agent_platform.prompt_files validate` to validate the same immutable
prompt catalog used by startup. The distribution name is `agent-platform`; its wheel contains
all three Python packages, migrations/Lua files and application prompt assets.
