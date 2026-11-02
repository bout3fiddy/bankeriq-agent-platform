"""Fixed-profile preparation preserves recoverable instructions outside conversation history."""

from dataclasses import replace
from typing import Any

import pytest

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.prompts.resolution import FixedPromptResolver, PromptUnavailableError
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import StateConflictError
from agent_runtime.storage.memory import InMemoryStateStore
from agent_runtime.tools.results import PendingToolCall, WaitReason


def test_fixed_preparation_retry_and_changed_startup_instructions() -> None:
    store = InMemoryStateStore()
    context = RunContext("tenant", "user", "chat", "run")
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    checkpoint = store.create_run(
        context, expected_conversation_revision=0, items=(Message(MessageRole.USER, "hello"),)
    )
    resolver = FixedPromptResolver("Exact synthetic startup instructions.")
    parameters: dict[str, Any] = dict(
        expected_conversation_revision=checkpoint.conversation.revision,
        expected_run_revision=checkpoint.run.revision,
        status=RunStatus.RUNNING,
        model_steps=1,
        prompt_resolver=resolver,
    )
    prepared = store.save_checkpoint(context, **parameters)
    assert store.save_checkpoint(context, **parameters) == prepared
    assert store.load_model_step(context, 1) == prepared
    assert prepared.run.prompt_manifest is not None
    assert (
        resolver.instructions(prepared.run.prompt_manifest)
        == "Exact synthetic startup instructions."
    )
    assert prepared.conversation.items == checkpoint.conversation.items

    with pytest.raises(PromptUnavailableError):
        FixedPromptResolver("Different startup").instructions(prepared.run.prompt_manifest)

    with pytest.raises(StateConflictError):
        store.save_checkpoint(context, **{**parameters, "model_steps": 2})

    with pytest.raises(LookupError):
        store.load_model_step(replace(context, user_id="other"), 1)


def test_pending_work_cannot_be_skipped_by_prompt_preparation() -> None:
    store = InMemoryStateStore()
    context = RunContext("tenant", "user", "chat", "run")
    store.create_conversation(tenant_id="tenant", user_id="user", conversation_id="chat")
    checkpoint = store.create_run(context, expected_conversation_revision=0)
    checkpoint = store.save_checkpoint(
        context, expected_conversation_revision=1, expected_run_revision=0, status=RunStatus.RUNNING
    )
    checkpoint = store.save_checkpoint(
        context,
        expected_conversation_revision=2,
        expected_run_revision=1,
        status=RunStatus.WAITING,
        pending_calls=(PendingToolCall("call", WaitReason.JOB, "job"),),
    )

    with pytest.raises(StateConflictError, match="resolved tool calls"):
        store.save_checkpoint(
            context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            status=RunStatus.RUNNING,
            model_steps=1,
            prompt_resolver=FixedPromptResolver(),
        )
