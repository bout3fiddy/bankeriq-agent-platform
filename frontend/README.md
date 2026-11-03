# Agent platform frontend

This directory is reserved for the browser application. No frontend is implemented yet.
Its future JavaScript dependencies and build will be separate from Python packaging.

The client will own chat rendering, conversation navigation, streamed provisional/final
answers, approval interactions and workforce sign-in UI. The backend lives in
[`src/agent_platform/`](../src/agent_platform/README.md). All model/tool access and policy
decisions pass through authenticated backend routes. Restricted reports retain their
separate authenticated delivery flow.

Do not infer a settled frontend framework or working sign-in screen from this directory.
See [components.md](../components.md#browser-chat-client) for the UI boundary.
