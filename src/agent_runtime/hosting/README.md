# Hosting boundary

`RuntimeEndpoint` connects validated application requests to the coordinator and
continuation handler. The Foundry wrapper in `../providers/foundry/hosting.py` exposes it
through Microsoft's Invocations SDK. Local tests exercise the actual ASGI server
with synthetic authentication, input policy, model replies and tool services.

```text
POST /invocations
  -> authenticate caller
  -> validate command
  -> worker thread: bind identity, check ownership, apply input policy
  -> coordinator / continuation handler
  -> JSON: answer, run status, pending events, separate artifact references
```

Invocations supports our custom status and delivery fields, while the existing
store owns conversation history. The model adapter separately uses Chat
Completions for inference. These are two independent API boundaries.

## Commands

All commands go to `POST /invocations`. The caller's tenant and user come from
the injected authentication provider. Extra JSON fields are rejected, including
identity, permissions, tools, approval decisions and worker output.

Create an empty conversation owned by the authenticated caller:

```json
{"action": "create_conversation"}
```

The response contains a generated `conversation_id` and `revision: 0`. Start a
user turn with a client-generated run ID and the last observed revision:

```json
{
  "action": "start_run",
  "conversation_id": "<returned conversation ID>",
  "run_id": "<fresh UUID for this user turn>",
  "expected_conversation_revision": 0,
  "message": "Calculate the total"
}
```

The response includes `status`, `answer`, `conversation_revision`, `pending` and
`artifacts`. The returned revision comes from that run's checkpoint. A later
turn uses it as `expected_conversation_revision`. Duplicate run IDs and stale
revisions return HTTP 409, with no automatic replay of tools.

When a trusted approval/job provider has recorded a result, send its notification:

```json
{
  "action": "resume_run",
  "conversation_id": "<conversation ID>",
  "run_id": "<waiting run ID>",
  "call_id": "<pending call ID>",
  "reason": "job",
  "reference_id": "<pending reference ID>"
}
```

The continuation resolver looks up the actual outcome and rechecks policy. A
notification alone cannot grant approval or supply a tool result. The response
can remain waiting when another job is outstanding.

Artifact IDs are returned separately for the authenticated application to retain
and deliver. They are not download authorizations. Actual report serving,
persisted delivery metadata and retrieval of lost responses remain future work.

## Wiring

Application startup can select this host independently from model inference
through `HOSTING_PROVIDER=foundry`; see the [provider guide](../providers/README.md).
The direct construction below is useful when supplying an already-built runtime.

Application startup supplies the configured runtime and two required dependencies:

```python
from agent_runtime.providers.foundry.hosting import create_hosted_server
from agent_runtime.hosting.endpoint import RuntimeEndpoint

endpoint = RuntimeEndpoint(
    store,
    coordinator,
    continuations,
    prepare_input=application_input_policy,
)
server = create_hosted_server(endpoint, authenticate=verify_application_identity)
server.run()
```

`authenticate(request)` is async and returns `Principal(tenant_id, user_id)` or
raises `AuthenticationRequired`. Production verification must map trusted
platform identity into enterprise identity. The adapter never directly trusts
body fields or caller-controlled headers as identity. The local fake tokens used
in tests have no production meaning.

`prepare_input(principal, text)` returns text admitted for the external model or
raises `InputRejected`. It runs after conversation ownership is checked and
before any message/run is saved. Raw chat admission policy remains unresolved;
tests inject a synthetic policy explicitly.

The host bounds request bodies to 64 KiB and message text to 32,000 characters.
Responses set `Cache-Control: no-store`. Application errors use fixed codes:
400 invalid request, 401 authentication required, 403 input rejected, 404 state
unavailable, 409 state conflict and 500 runtime unavailable. A valid invocation
returns HTTP 200 with its run status, which may itself be failed or waiting.

## Execution and validation limits

The async HTTP handler offloads synchronous coordinator work to Starlette's
worker pool. Independent sessions can proceed concurrently; revision checks
reserve one active run per conversation. Model calls and tool dispatch within
each run remain synchronous. PostgreSQL job transport, durable recovery,
streaming and request-disconnect cancellation remain unimplemented.

Microsoft's SDK supplies `/readiness`, server startup and platform integration.
SDK-managed observability is disabled in this wrapper because its automatic
message capture needs a separate data policy. Existing application-level
instrumentation still requires review before deployment.

Tests cover the full HTTP approval/report workflow, identity isolation, input
admission, sanitized errors, stale requests and overlapping model calls from two
sessions. Live Azure identity propagation, network controls, container lifecycle,
shutdown handling and deployment remain untested. The application owns the
lifetimes of its model/database clients and must close them during shutdown.

Run the local suite with:

```sh
uv run python -m pytest tests/unit/hosting tests/integration/test_hosting.py -q
uv run python scripts/verify.py
```

## AWS option

AWS could use the same coordinator, registry, gateway, output policy and runtime
request handler. This is a proposed alternative; AWS adapters are unimplemented.

| Responsibility | Possible AWS implementation |
| --- | --- |
| Host our Python runtime | Bedrock AgentCore Runtime with an AWS hosting wrapper |
| Model inference | A `ModelClient` adapter calling Bedrock Converse through Boto3 |
| Tool applications | ECS tasks on Fargate, reached through our existing gateway |
| Restricted reports | Private S3 objects served through our authenticated application |

The Bedrock adapter would map `toolUse` to `ModelReply.calls`, return enforced
results as `toolResult`, and map a completed turn to `ModelReply.answer`. Model
capabilities, reasoning options and continuation requirements need verification
for the selected deployment. AgentCore has its own health, session, identity and
container requirements. Persisted state and durable execution remain separate
implementation work on either cloud.

Sources checked on 30 October 2026:

- [Foundry hosting requirements][line-source-1]
- [AgentCore custom agent hosting][line-source-2]
- [Bedrock client-side tool use][line-source-3]
- [AgentCore HTTP requirements][line-source-4]
- [ECS container hosting](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html)
- [S3 access controls](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-management.html)

[line-source-1]:
  https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract
[line-source-2]:
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html
[line-source-3]:
  https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-client-side.html
[line-source-4]:
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
