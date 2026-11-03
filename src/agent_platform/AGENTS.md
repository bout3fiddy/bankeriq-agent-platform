# Agent platform backend guidance

Read the root instructions and [README.md](README.md).

- Own application configuration/composition, HTTP routes, identity, prompt asset selection
  and application-specific tool bindings. Import harness/runtime components and inject
  dependencies explicitly. Reusable execution belongs in `agent_runtime`.
- Authenticate before constructing trusted tenant/user context. Input JSON and model
  output cannot grant permissions or choose credentials/providers. Enforce scoped access
  on history, progress, admission, approval, recovery and delivery routes.
- Keep the API responsive: durable acceptance finishes before returning an accepted ID;
  workers execute the model/tool work. Keep authentication and policy adapters outside
  the harness. Preserve the existing operator-only input restriction until separately changed.
- Load application prompt assets here, then pass `PromptCatalog` into runtime storage.
  Keep pinned asset contents and identities unchanged during structural refactors.
- The browser application belongs in `frontend/`. Synthetic tools remain in
  `tests/validation/`; production application tools belong under `tools/` when implemented.
- Existing environment-variable names and cloud resource identifiers remain compatibility
  inputs. Renaming a Python package does not authorise changing or deploying infrastructure.
