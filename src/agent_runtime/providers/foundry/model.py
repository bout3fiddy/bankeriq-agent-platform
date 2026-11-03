"""Synchronous Foundry model adapter for the coordinator's ModelClient interface."""

import json
import math
from collections.abc import Callable
from time import monotonic
from typing import cast

from openai import APIError, APIStatusError, AsyncOpenAI, OpenAI, omit
from openai.types.chat import ChatCompletion, ChatCompletionFunctionToolParam
from openai.types.shared import ReasoningEffort as SdkReasoningEffort

from agent_harness.coordinator.model import (
    ModelContextOverflow,
    ModelRateLimited,
    ModelReply,
    ModelRequest,
    ReasoningEffort,
)
from agent_runtime.providers.foundry.messages import encode_json, encode_messages
from agent_runtime.providers.foundry.metrics import ModelObservation, observe
from agent_runtime.providers.foundry.replies import FoundryModelError, decode_reply


class FoundryModelClient:
    """Make one inference request using the history and tools supplied for this step."""

    def __init__(
        self,
        client: OpenAI,
        *,
        deployment: str,
        supported_reasoning_efforts: frozenset[ReasoningEffort] = frozenset(),
        max_completion_tokens: int = 4096,
        observer: Callable[[ModelObservation], None] | None = None,
    ) -> None:

        # One complete() call consumes one coordinator model-step attempt.
        # Disable the SDK's automatic retries even for an injected client so
        # rate limits/timeouts cannot silently multiply those attempts.
        self._client = client.with_options(max_retries=0)
        self._deployment = deployment
        self._supported_reasoning_efforts = supported_reasoning_efforts
        self._max_completion_tokens = max_completion_tokens
        self._observer = observer

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
        started = monotonic()

        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self._deployment,
                messages=messages,
                tools=tools if tools else omit,
                tool_choice="auto" if tools else omit,
                parallel_tool_calls=True if tools else omit,
                reasoning_effort=cast(SdkReasoningEffort, effort.value) if effort else omit,
                max_completion_tokens=_output_limit(request, self._max_completion_tokens),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "runtime_output",
                        "strict": True,
                        "schema": json.loads(encode_json(request.output_schema)),
                    },
                }
                if request.output_schema is not None
                else omit,
                store=False,
                stream=False,
            )

            response = raw.parse()

            # SDK response parsing normally tolerates missing/wrong field types.
            # Validate the external envelope before treating it as typed data.
            # Serialization warnings can quote raw values; validation below
            # still rejects them, with a fixed error message for the caller.
            response = ChatCompletion.model_validate(
                response.model_dump(warnings=False), strict=True
            )
            observe(
                self._observer,
                started,
                raw.status_code,
                request.output_schema is not None,
                raw.headers,
                response,
            )
        except APIError as error:
            observe(
                self._observer,
                started,
                error.status_code if isinstance(error, APIStatusError) else 0,
                request.output_schema is not None,
                error.response.headers if isinstance(error, APIStatusError) else {},
            )
            raise _failure(error) from None
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
        observer: Callable[[ModelObservation], None] | None = None,
    ) -> None:

        # One complete() call consumes one coordinator model-step attempt.
        # Disable the SDK's automatic retries even for an injected client so
        # rate limits/timeouts cannot silently multiply those attempts.
        self._client = client.with_options(max_retries=0)
        self._deployment = deployment
        self._supported_reasoning_efforts = supported_reasoning_efforts
        self._max_completion_tokens = max_completion_tokens
        self._observer = observer

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
        started = monotonic()

        try:
            raw = await self._client.chat.completions.with_raw_response.create(
                model=self._deployment,
                messages=messages,
                tools=tools if tools else omit,
                tool_choice="auto" if tools else omit,
                parallel_tool_calls=True if tools else omit,
                reasoning_effort=cast(SdkReasoningEffort, effort.value) if effort else omit,
                max_completion_tokens=_output_limit(request, self._max_completion_tokens),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "runtime_output",
                        "strict": True,
                        "schema": json.loads(encode_json(request.output_schema)),
                    },
                }
                if request.output_schema is not None
                else omit,
                store=False,
                stream=False,
            )

            response = raw.parse()

            # SDK response parsing normally tolerates missing/wrong field types.
            # Validate the external envelope before treating it as typed data.
            # Serialization warnings can quote raw values; validation below
            # still rejects them, with a fixed error message for the caller.
            response = ChatCompletion.model_validate(
                response.model_dump(warnings=False), strict=True
            )
            observe(
                self._observer,
                started,
                raw.status_code,
                request.output_schema is not None,
                raw.headers,
                response,
            )
        except APIError as error:
            observe(
                self._observer,
                started,
                error.status_code if isinstance(error, APIStatusError) else 0,
                request.output_schema is not None,
                error.response.headers if isinstance(error, APIStatusError) else {},
            )
            raise _failure(error) from None
        except ValueError:
            raise FoundryModelError("invalid Foundry response") from None

        return decode_reply(response)


def _output_limit(request: ModelRequest, configured: int) -> int:
    if request.max_output_tokens is None:
        return configured

    if request.max_output_tokens < 1:
        raise FoundryModelError("invalid request output limit")

    return min(configured, request.max_output_tokens)


def _failure(error: APIError) -> RuntimeError:

    # Only an explicit HTTP 400 code proves a clean context rejection. A timeout,
    # malformed response or output-length finish cannot consume overflow recovery.
    # Raw provider messages can contain prompts; copy no text into outward errors.
    if isinstance(error, APIStatusError):
        if error.status_code == 400 and error.code == "context_length_exceeded":
            return ModelContextOverflow("provider_context_overflow")

        if error.status_code == 429:
            headers = error.response.headers

            try:
                delay = (
                    float(headers["retry-after-ms"]) / 1000
                    if "retry-after-ms" in headers
                    else float(headers.get("retry-after", "2"))
                )
            except ValueError:
                delay = 2

            if not math.isfinite(delay):
                delay = 2

            return ModelRateLimited(min(300, max(0.1, delay)))

    return FoundryModelError("Foundry inference request failed")
