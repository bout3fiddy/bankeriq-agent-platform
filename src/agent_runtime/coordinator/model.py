"""Provider-independent requests and replies for one model step."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..conversations.conversation import ConversationItem
from ..prompts.resolution import PromptManifest
from ..tools.definitions import ToolCall
from ..types.json_value import JsonValue


class ReasoningEffort(StrEnum):
    """Requested reasoning effort; available choices depend on the deployed model."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class ModelTool:
    """The name, description and schema exposed to the model."""

    name: str
    description: str
    argument_schema: Mapping[str, JsonValue] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Permitted history and tools for one model call."""

    history: tuple[ConversationItem, ...] = field(repr=False)
    tools: tuple[ModelTool, ...]

    # Example: HIGH requests extra provider-managed reasoning. Each adapter maps
    # that intent to its API and rejects settings unsupported by its model.
    # Python None leaves the provider default unchanged. ReasoningEffort.NONE
    # explicitly requests disabled reasoning where the deployed model supports it.
    reasoning_effort: ReasoningEffort | None = None

    # Example: the prepared checkpoint holds a v7 manifest, and the resolver
    # returns its composed text. Supply both on this request even if v8 became
    # active meanwhile. Adapters send the text as instructions; the manifest is
    # available to application recovery and stays outside conversation history.
    instructions: str = field(default="", repr=False)
    prompt_manifest: PromptManifest | None = None


@dataclass(frozen=True, slots=True)
class ModelReply:
    """A final answer or a batch of tool requests; the coordinator checks the shape."""

    answer: str | None = field(default=None, repr=False)
    calls: tuple[ToolCall, ...] = ()


class ModelClient(Protocol):
    """An adapter supplies a complete reply for each request.

    Map reasoning_effort to the provider API and reject unsupported model/effort
    combinations. SDK objects, credentials and provider response formats belong
    to adapters. An adapter that retains provider continuation state must scope
    it to the conversation/run.
    """

    # A scripted client can return ModelReply(calls=(calculate_call,)) on its
    # first invocation, then ModelReply(answer="The total is 42") after the
    # gateway's visible result appears in the next request's history.
    def complete(self, request: ModelRequest) -> ModelReply:
        """Run one model step; provider errors may raise exceptions."""

        ...


class AsyncModelClient(Protocol):
    """Application adapters own async transports, credentials and finite timeouts.

    For example, async def complete(request) can await a local HTTP provider and
    return ModelReply(calls=(call_a, call_b)). The runtime awaits one bounded step;
    it never retains that coroutine while tool jobs or approvals are waiting.
    """

    async def complete(self, request: ModelRequest) -> ModelReply: ...
