# Foundry model adapter

This provider is selected by application startup through `MODEL_PROVIDER=foundry`.
See the [provider guide](../README.md) for shared configuration, independent
hosting selection, SDK extras and connection lifetime. The direct SDK wiring
below is also available for focused integration tests and custom credential wiring.

`FoundryModelClient` implements `ModelClient.complete()` using the OpenAI Python
SDK and Foundry's v1 Chat Completions endpoint:

```text
Coordinator -> ModelRequest(history, permitted tools, reasoning effort)
            -> FoundryModelClient -> POST /openai/v1/chat/completions
            <- ModelReply(answer=...) or ModelReply(calls=(...))
```

The existing coordinator executes returned calls through the gateway, saves their
enforced results and invokes the adapter again. Each request replays its supplied
history. The adapter retains no conversation or run cache, so an instance can
serve separate sessions without mixing their transcripts.

## API mapping

| Runtime value | API representation |
| --- | --- |
| `Message` | User or assistant text |
| Consecutive `ToolCall` records | One assistant message with `tool_calls` |
| `VisibleResult` | Tool message with `{"status": "completed", "result": ...}` |
| `RestrictedReceipt` | Tool message with `{"status": "completed", "output": "restricted"}` |
| `ToolFailure` | Tool message with fixed status and error code |
| Pending approval/job records | Omitted; the eventual result answers the call |
| Call with no saved result | Fixed `{"status": "unresolved"}` tool message |

An interrupted earlier run can leave unanswered calls in conversation history.
The unresolved marker makes that uncertainty explicit when another user turn
replays the history. Approval IDs, job IDs and artifact references stay outside
the API payload. Admission of message text and visible result content remains
the input/output policy's responsibility.

`finish_reason="tool_calls"` produces `reply.calls`, including several calls from
one response. Accompanying commentary is omitted. `finish_reason="stop"` with
text or a refusal produces `reply.answer`. Truncation, filtering, malformed
responses and invalid argument JSON raise `FoundryModelError`; the coordinator
records a failed run. The gateway still validates operation arguments and access.

## Connection and deployment configuration

The application supplies trusted configuration and credentials at startup:

```python
import os

from agent_runtime.providers.foundry.connection import create_foundry_client
from agent_runtime.providers.foundry.model import FoundryModelClient

with create_foundry_client(
    base_url=os.environ["FOUNDRY_OPENAI_BASE_URL"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
) as client:
    model = FoundryModelClient(
        client,
        deployment=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
    )
    # Pass model and a PromptResolver into Coordinator(..., model=model, prompts=resolver, ...).
    # Keep the client open for the coordinator's lifetime, including resumed runs.
```

`FOUNDRY_OPENAI_BASE_URL` is the full resource API base URL, for example
`https://RESOURCE.openai.azure.com/openai/v1/`. `deployment` is the configured
Azure model deployment name. `api_key` also accepts a renewable token-provider
callable supplied by the application. The startup factory supports
`FOUNDRY_AUTH_MODE=azure_cli`, which obtains renewable Entra tokens from the
signed-in Azure CLI identity. Managed identity acquisition remains future work.
Client construction makes no inference call.

The connection defaults to a 60-second SDK request timeout. Automatic SDK retries
are disabled, including for injected clients, so one coordinator attempt makes
one inference request. The request uses `store=False`; service retention and
telemetry policy still need validation for the selected Azure deployment.

Reasoning defaults to the provider's setting. To enable an explicit effort, set
`supported_reasoning_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH})`
on the adapter after verifying those choices for the deployment with function
calling. The coordinator's setting becomes the API's `reasoning_effort` field.
An unlisted explicit effort fails before a network call. Deployment aliases
cannot reliably identify supported model features.

## Validation and current limits

Tests use the real SDK with an in-process HTTP transport. They check serialized
requests, final answers, repeated tool calls, approval/job resumption, current
tool discovery, output separation, malformed replies, timeouts and HTTP failures.
No Azure account, credentials or live endpoint were used for these tests.

Separately, the executable demo passed live inference on 2 November 2026 using
`gpt-4o` version `2024-11-20`, regional Standard in Sweden Central, and Azure CLI
Entra credentials. It completed four model calls, three successive synthetic tool
calls and one continuation, with restricted output excluded from model history.
See the [provisioning and validation record](../../../../infra/azure/README.md#validation-evidence).

Live use requires an Azure subscription/resource, a compatible deployed model,
its endpoint and authorized credentials. Azure DevOps is a separate development
service and is not a prerequisite for this inference API.

This adapter supports synchronous, non-streaming, text/function Chat Completions.
Model selection must verify support for this combination and the requested
reasoning effort. Responses API continuation, streaming, context compaction,
asynchronous model calls remain future work. The separate
[hosting adapter](../../hosting/README.md) implements Hosted Agents Invocations
request handling with local ASGI tests and synthetic identity providers.

Sources checked on 30 October 2026:

- [Microsoft function-calling guide][line-source-1]
- [Microsoft v1 Chat Completions reference][line-source-2]
- [Microsoft reasoning-model compatibility][line-source-3]
- [OpenAI Chat Completions reference][line-source-4]

Instructions now come from each `ModelRequest`, resolved and checkpointed before
inference. The client retains no startup instruction string. See the
[shared prompt guide](../../prompts/README.md) for fixed/shared resolver composition
and prepared-step recovery. Local HTTP tests cover concurrent requests through a
shared client; live Foundry propagation remains unvalidated.

[line-source-1]:
  https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/function-calling
[line-source-2]:
  https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat
[line-source-3]:
  https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning
[line-source-4]:
  https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
