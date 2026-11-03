"""Synchronous Foundry model adapter for the coordinator's ModelClient interface."""

import json
from typing import cast

from openai import APIError, AsyncOpenAI, OpenAI, omit
from openai.types.chat import ChatCompletion, ChatCompletionFunctionToolParam
from openai.types.shared import ReasoningEffort as SdkReasoningEffort

from ...coordinator.model import ModelReply, ModelRequest, ReasoningEffort
from .messages import encode_json, encode_messages
from .replies import FoundryModelError, decode_reply


class FoundryModelClient:
    """Make one inference request using the history and tools supplied for this step."""

    def __init__(
        self,
        client: OpenAI,
        *,
        deployment: str,
        supported_reasoning_efforts: frozenset[ReasoningEffort] = frozenset(),
        max_completion_tokens: int = 4096,
    ) -> None:

        # One complete() call consumes one coordinator model-step attempt.
        # Disable the SDK's automatic retries even for an injected client so
        # rate limits/timeouts cannot silently multiply those attempts.
        self._client = client.with_options(max_retries=0)
        self._deployment = deployment
        self._supported_reasoning_efforts = supported_reasoning_efforts
        self._max_completion_tokens = max_completion_tokens

    def complete(self, request: ModelRequest) -> ModelReply:
        """Translate runtime inputs, call Foundry, then validate its next action."""

        # Azure deployment names are chosen by the application, e.g. "coordinator".
        # They do not identify model capabilities. Startup supplies the efforts
        # verified for that deployment with function calling enabled.
        effort = request.reasoning_effort

        if effort is not None and effort not in self._supported_reasoning_efforts:
            raise FoundryModelError("reasoning effort is unsupported by this deployment")

        # Example: requests A and B share this client but carry instructions
        # from v7 and v8 respectively. Build a fresh message list for each call;
        # inserting A's system text cannot change B's request or stored history.
        # The manifest remains runtime metadata; only the resolved text enters
        # the provider's system message.
        messages = encode_messages(request.history)
        messages.insert(0, {"role": "system", "content": request.instructions})

        # strict=False preserves registry schemas with optional arguments.
        # The gateway's validator checks actual arguments before execution.
        tools: list[ChatCompletionFunctionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": json.loads(encode_json(tool.argument_schema)),
                    "strict": False,
                },
            }
            for tool in request.tools
        ]

        # POST /openai/v1/chat/completions. Only current discovery results are
        # sent as tools; permissions, bindings and credentials stay in Python.
        # "auto" lets the model answer or ask for another batch of tool calls.
        # store=False opts out of stored completions; Azure retention and logging
        # configuration still require separate review before live use.
        try:
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                tools=tools if tools else omit,
                tool_choice="auto" if tools else omit,
                parallel_tool_calls=True if tools else omit,
                reasoning_effort=cast(SdkReasoningEffort, effort.value) if effort else omit,
                max_completion_tokens=self._max_completion_tokens,
                store=False,
                stream=False,
            )

            # SDK response parsing normally tolerates missing/wrong field types.
            # Validate the external envelope before treating it as typed data.
            # Serialization warnings can quote raw values; validation below
            # still rejects them, with a fixed error message for the caller.
            response = ChatCompletion.model_validate(
                response.model_dump(warnings=False), strict=True
            )
        except APIError:
            raise FoundryModelError("Foundry inference request failed") from None
        except ValueError:
            raise FoundryModelError("invalid Foundry response") from None

        return decode_reply(response)


class AsyncFoundryModelClient:
    """Make one inference request using the history and tools supplied for this step."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        deployment: str,
        supported_reasoning_efforts: frozenset[ReasoningEffort] = frozenset(),
        max_completion_tokens: int = 4096,
    ) -> None:

        # One complete() call consumes one coordinator model-step attempt.
        # Disable the SDK's automatic retries even for an injected client so
        # rate limits/timeouts cannot silently multiply those attempts.
        self._client = client.with_options(max_retries=0)
        self._deployment = deployment
        self._supported_reasoning_efforts = supported_reasoning_efforts
        self._max_completion_tokens = max_completion_tokens

    async def complete(self, request: ModelRequest) -> ModelReply:
        """Translate runtime inputs, call Foundry, then validate its next action."""

        # Azure deployment names are chosen by the application, e.g. "coordinator".
        # They do not identify model capabilities. Startup supplies the efforts
        # verified for that deployment with function calling enabled.
        effort = request.reasoning_effort

        if effort is not None and effort not in self._supported_reasoning_efforts:
            raise FoundryModelError("reasoning effort is unsupported by this deployment")

        # Example: requests A and B share this client but carry instructions
        # from v7 and v8 respectively. Build a fresh message list for each call;
        # inserting A's system text cannot change B's request or stored history.
        # The manifest remains runtime metadata; only the resolved text enters
        # the provider's system message.
        messages = encode_messages(request.history)
        messages.insert(0, {"role": "system", "content": request.instructions})

        # strict=False preserves registry schemas with optional arguments.
        # The gateway's validator checks actual arguments before execution.
        tools: list[ChatCompletionFunctionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": json.loads(encode_json(tool.argument_schema)),
                    "strict": False,
                },
            }
            for tool in request.tools
        ]

        # POST /openai/v1/chat/completions. Only current discovery results are
        # sent as tools; permissions, bindings and credentials stay in Python.
        # "auto" lets the model answer or ask for another batch of tool calls.
        # store=False opts out of stored completions; Azure retention and logging
        # configuration still require separate review before live use.
        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                tools=tools if tools else omit,
                tool_choice="auto" if tools else omit,
                parallel_tool_calls=True if tools else omit,
                reasoning_effort=cast(SdkReasoningEffort, effort.value) if effort else omit,
                max_completion_tokens=self._max_completion_tokens,
                store=False,
                stream=False,
            )

            # SDK response parsing normally tolerates missing/wrong field types.
            # Validate the external envelope before treating it as typed data.
            # Serialization warnings can quote raw values; validation below
            # still rejects them, with a fixed error message for the caller.
            response = ChatCompletion.model_validate(
                response.model_dump(warnings=False), strict=True
            )
        except APIError:
            raise FoundryModelError("Foundry inference request failed") from None
        except ValueError:
            raise FoundryModelError("invalid Foundry response") from None

        return decode_reply(response)
