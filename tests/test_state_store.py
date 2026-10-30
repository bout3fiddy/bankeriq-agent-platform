"""Ownership, atomic updates and lifecycle behaviour of the in-memory store."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import cast

import pytest

from agent_runtime.conversations.conversation import ConversationItem
from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.storage.interface import (
    Checkpoint,
    StateConflictError,
    StateNotFoundError,
    StateStore,
)
from agent_runtime.storage.memory import InMemoryStateStore
from agent_runtime.tools.definitions import ToolCall
from agent_runtime.tools.results import (
    ArtifactReference,
    PendingToolCall,
    RestrictedReceipt,
    WaitReason,
)

CONTEXT = RunContext("tenant-1", "user-1", "conversation-1", "run-1")


def start_run() -> tuple[StateStore, Checkpoint]:
    store: StateStore = InMemoryStateStore()
    conversation = store.create_conversation(
        tenant_id=CONTEXT.tenant_id,
        user_id=CONTEXT.user_id,
        conversation_id=CONTEXT.conversation_id,
    )
    checkpoint = store.create_run(
        CONTEXT,
        expected_conversation_revision=conversation.revision,
        items=(Message(MessageRole.USER, "Prepare a synthetic report"),),
    )
    return store, checkpoint


def save(
    store: StateStore,
    checkpoint: Checkpoint,
    status: RunStatus,
    items: tuple[ConversationItem, ...] = (),
    pending: tuple[PendingToolCall, ...] = (),
) -> Checkpoint:
    return store.save_checkpoint(
        checkpoint.run.context,
        expected_conversation_revision=checkpoint.conversation.revision,
        expected_run_revision=checkpoint.run.revision,
        status=status,
        items=items,
        pending_calls=pending,
    )


def test_tool_completion_updates_history_and_run_together() -> None:
    store, queued = start_run()
    call = ToolCall("call-1", "report", {})
    running = save(store, queued, RunStatus.RUNNING, (call,))
    waiting = save(
        store,
        running,
        RunStatus.WAITING,
        pending=(PendingToolCall("call-1", WaitReason.JOB, "job-1"),),
    )
    receipt = RestrictedReceipt("call-1")
    resumed = save(store, waiting, RunStatus.RUNNING, (receipt,))
    completed = save(
        store, resumed, RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, "Ready"),)
    )

    assert completed == store.load_run(CONTEXT)
    assert completed.conversation.items[1:] == (
        call,
        receipt,
        Message(MessageRole.ASSISTANT, "Ready"),
    )
    assert completed.conversation.active_run_id is None
    assert completed.run.pending_calls == ()
    assert completed.run.revision == 4
    assert waiting.run.status is RunStatus.WAITING
    assert len(queued.conversation.items) == 1


@pytest.mark.parametrize(
    "change", [{"user_id": "other"}, {"tenant_id": "other"}, {"conversation_id": "other"}]
)
def test_ownership_checks_apply_to_reads_and_writes(change: dict[str, str]) -> None:
    store, checkpoint = start_run()
    other = replace(CONTEXT, **change)

    with pytest.raises(StateNotFoundError):
        store.load_run(other)

    with pytest.raises(StateNotFoundError):
        store.load_conversation(
            tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
        )

    with pytest.raises(StateNotFoundError):
        store.save_checkpoint(
            other,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            status=RunStatus.RUNNING,
        )

    assert store.load_run(CONTEXT) == checkpoint


def test_tenants_can_reuse_conversation_and_run_ids() -> None:
    store, original = start_run()
    other = replace(CONTEXT, tenant_id="tenant-2")
    store.create_conversation(
        tenant_id=other.tenant_id, user_id=other.user_id, conversation_id=other.conversation_id
    )
    second = store.create_run(other, expected_conversation_revision=0)
    assert second.run.context == other
    assert store.load_run(CONTEXT) == original


def test_stale_checkpoint_preserves_both_records() -> None:
    store, queued = start_run()
    running = save(store, queued, RunStatus.RUNNING)

    with pytest.raises(StateConflictError):
        save(store, queued, RunStatus.RUNNING, (Message(MessageRole.ASSISTANT, "stale"),))

    assert store.load_run(CONTEXT) == running


@pytest.mark.parametrize("conversation_delta,run_delta", [(1, 0), (0, 1)])
def test_each_revision_is_checked(conversation_delta: int, run_delta: int) -> None:
    store, checkpoint = start_run()

    with pytest.raises(StateConflictError):
        store.save_checkpoint(
            CONTEXT,
            expected_conversation_revision=checkpoint.conversation.revision + conversation_delta,
            expected_run_revision=checkpoint.run.revision + run_delta,
            status=RunStatus.RUNNING,
        )

    assert store.load_run(CONTEXT) == checkpoint


def test_invalid_wait_state_rolls_back_history_append() -> None:
    store, queued = start_run()
    running = save(store, queued, RunStatus.RUNNING)

    with pytest.raises(ValueError):
        save(store, running, RunStatus.WAITING, (Message(MessageRole.ASSISTANT, "partial"),))

    assert store.load_run(CONTEXT) == running


def test_artifact_reference_cannot_enter_history() -> None:
    store, queued = start_run()
    artifact = ArtifactReference("call-1", "PRIVATE_ARTIFACT")

    with pytest.raises(ValueError, match="unsupported conversation item"):
        save(store, queued, RunStatus.RUNNING, (cast(ConversationItem, artifact),))

    assert store.load_run(CONTEXT) == queued


def test_invalid_history_does_not_create_a_run_or_claim_conversation() -> None:
    store = InMemoryStateStore()
    conversation = store.create_conversation(tenant_id="t", user_id="u", conversation_id="c")
    context = RunContext("t", "u", "c", "r")

    with pytest.raises(ValueError):
        store.create_run(
            context, expected_conversation_revision=0, items=(cast(ConversationItem, object()),)
        )

    assert store.load_conversation(tenant_id="t", user_id="u", conversation_id="c") == conversation
    assert (
        store.create_run(context, expected_conversation_revision=0).run.status is RunStatus.QUEUED
    )


def test_active_run_blocks_another_until_completion() -> None:
    store, queued = start_run()
    next_context = replace(CONTEXT, run_id="run-2")

    with pytest.raises(StateConflictError):
        store.create_run(next_context, expected_conversation_revision=queued.conversation.revision)

    completed = save(store, save(store, queued, RunStatus.RUNNING), RunStatus.COMPLETED)
    next_run = store.create_run(
        next_context, expected_conversation_revision=completed.conversation.revision
    )
    assert next_run.conversation.active_run_id == "run-2"
    assert next_run.run.status is RunStatus.QUEUED

    with pytest.raises(StateConflictError):
        save(store, completed, RunStatus.RUNNING)


def test_duplicate_ids_cannot_overwrite_history() -> None:
    store, queued = start_run()

    with pytest.raises(StateConflictError):
        store.create_conversation(
            tenant_id=CONTEXT.tenant_id,
            user_id=CONTEXT.user_id,
            conversation_id=CONTEXT.conversation_id,
        )

    cancelled = save(store, queued, RunStatus.CANCELLED)

    with pytest.raises(StateConflictError):
        store.create_run(CONTEXT, expected_conversation_revision=cancelled.conversation.revision)

    assert store.load_run(CONTEXT) == cancelled


def test_queued_run_cannot_skip_execution() -> None:
    store, queued = start_run()

    with pytest.raises(ValueError, match="invalid run transition"):
        save(store, queued, RunStatus.COMPLETED)

    assert store.load_run(CONTEXT) == queued


def test_only_one_competing_checkpoint_succeeds() -> None:
    store, checkpoint = start_run()
    barrier = Barrier(2)

    def update(text: str) -> bool:
        barrier.wait(timeout=5)

        try:
            save(store, checkpoint, RunStatus.RUNNING, (Message(MessageRole.ASSISTANT, text),))
            return True
        except StateConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, ("first", "second")))

    final = store.load_run(CONTEXT)
    assert sorted(results) == [False, True]
    assert final.run.revision == 1
    assert len(final.conversation.items) == 2
