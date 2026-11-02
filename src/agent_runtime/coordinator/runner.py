"""Synchronous coordinator with model and gateway dependencies supplied by callers."""

from collections.abc import Callable
from typing import Protocol

from ..conversations.conversation import ConversationItem
from ..conversations.messages import Message, MessageRole
from ..policy.output import EnforcedOutput, enforce_failure
from ..prompts.resolution import FixedPromptResolver, PromptResolver, PromptUnavailableError
from ..runs.context import RunContext
from ..runs.state import RunStatus
from ..storage.interface import Checkpoint, StateConflictError, StateStore
from ..tools.definitions import ToolCall, ToolDefinition
from ..tools.registry import ToolRegistry
from ..tools.results import ArtifactReference, PendingToolCall, ToolErrorCode, ToolFailure
from .control import CoordinatorResult, RunLimits
from .model import ModelClient, ModelReply, ModelRequest, ModelTool, ReasoningEffort
from .reply_validation import is_valid_reply


class ToolDispatcher(Protocol):
    """The real gateway or a local fake supplies policy-enforced outcomes."""

    def dispatch(self, call: ToolCall, *, version: str, context: RunContext) -> EnforcedOutput:
        """Execute or return a pending receipt through the gateway boundary."""

        ...


class Coordinator:
    """Start queued runs and checkpoint the model/tool cycle until it stops."""

    def __init__(
        self,
        store: StateStore,
        registry: ToolRegistry,
        gateway: ToolDispatcher,
        model: ModelClient,
        permissions_for: Callable[[RunContext], frozenset[str]],
        limits: RunLimits | None = None,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        prompts: PromptResolver | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._gateway = gateway
        self._model = model
        self._permissions_for = permissions_for
        self._limits = limits if limits is not None else RunLimits()
        self._reasoning_effort = reasoning_effort
        self._prompts = prompts if prompts is not None else FixedPromptResolver()

    def start(
        self,
        context: RunContext,
        message: Message,
        *,
        expected_conversation_revision: int,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoordinatorResult:
        """Create and execute a run using a message already admitted by input policy."""

        checkpoint = self._store.create_run(
            context, expected_conversation_revision=expected_conversation_revision, items=(message,)
        )

        if (
            checkpoint.conversation.active_run_id != context.run_id
            or checkpoint.run.status is not RunStatus.QUEUED
        ):
            answer = next(
                (
                    item.text
                    for item in reversed(checkpoint.conversation.items)
                    if isinstance(item, Message) and item.role is MessageRole.ASSISTANT
                ),
                None,
            )
            return CoordinatorResult(
                checkpoint.run,
                answer if checkpoint.run.status is RunStatus.COMPLETED else None,
                self._store.deliveries(context),
                conversation_revision=checkpoint.conversation.revision,
            )

        return self.run(context, is_cancelled=is_cancelled)

    def run(
        self, context: RunContext, *, is_cancelled: Callable[[], bool] | None = None
    ) -> CoordinatorResult:
        """Claim a queued run and execute it once; storage conflicts propagate."""

        # start() saves the user's message first. load_run() checks ownership and
        # retrieves the conversation history and execution state for this context.
        checkpoint = self._store.load_run(context)

        # Re-entering a run could repeat side effects. The durable layer owns
        # matching and resolving events for waiting runs.
        if checkpoint.run.status is not RunStatus.QUEUED:
            raise StateConflictError("coordinator requires a queued run")

        # The revision check allows only one caller to claim this queued run.
        checkpoint = self._save(checkpoint, RunStatus.RUNNING)
        return self._run_loop(checkpoint, [], is_cancelled)

    def continue_run(
        self,
        checkpoint: Checkpoint,
        *,
        artifacts: tuple[ArtifactReference, ...] = (),
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoordinatorResult:
        """Continue a running checkpoint whose pending work was resolved by its owner."""

        # For example, the durable handler saved job-17's receipt in history and
        # now hands us its claimed checkpoint for the next model/tool iteration.
        return self._run_loop(checkpoint, list(artifacts), is_cancelled)

    def _run_loop(
        self,
        checkpoint: Checkpoint,
        artifacts: list[ArtifactReference],
        is_cancelled: Callable[[], bool] | None,
    ) -> CoordinatorResult:
        """Drive a claimed run, using its stored history and remaining budgets."""

        seen_call_ids = {
            item.call_id for item in checkpoint.conversation.items if isinstance(item, ToolCall)
        }

        # Each model step uses the history saved by the previous step:
        #
        #   history -> model -> answer -> save COMPLETED, return answer
        #                |
        #              calls -> gateway -> save tool results
        #                                    | completed: next model step
        #                                    | pending: save WAITING, return
        #
        # Example: calculate(count=2) -> {"total": 2} -> model uses that result
        # to request another tool or answer "The total is 2". The step budget
        # includes the model call that produces the answer.
        for _ in range(self._limits.model_steps - checkpoint.run.model_steps):

            # Example callback: is_cancelled=lambda: cancel_event.is_set().
            if is_cancelled is not None and is_cancelled():
                return self._finish(checkpoint, RunStatus.CANCELLED, artifacts)

            # Count the attempt before calling the provider. A wait/resume keeps
            # this count, so repeated job events cannot reset the model budget.
            checkpoint = self._save(
                checkpoint,
                RunStatus.RUNNING,
                model_steps=checkpoint.run.model_steps + 1,
                prompt_resolver=self._prompts,
            )

            try:
                reply, definitions = self._request_model(checkpoint)
            except PromptUnavailableError:
                raise
            except Exception:

                # Provider exceptions may contain private context; save status only.
                return self._finish(checkpoint, RunStatus.FAILED, artifacts)

            # Cancellation may arrive during the blocking model call.
            if is_cancelled is not None and is_cancelled():
                return self._finish(checkpoint, RunStatus.CANCELLED, artifacts)

            if not is_valid_reply(reply, seen_call_ids):
                return self._finish(checkpoint, RunStatus.FAILED, artifacts)

            # `is not None` is true when answer contains a value. For example,
            # ModelReply(answer="Total is 2") completes the run here.
            # ModelReply(calls=(calculate_call,)) has answer=None, so execution
            # continues below to run tools and feed their results back to the model.
            if reply.answer is not None:

                # Save the answer and completion together before returning them.
                checkpoint = self._save(
                    checkpoint, RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, reply.answer),)
                )
                return CoordinatorResult(
                    checkpoint.run,
                    reply.answer,
                    tuple(artifacts),
                    conversation_revision=checkpoint.conversation.revision,
                )

            # With two calls left, a three-call batch stops before any execution.
            if checkpoint.run.tool_calls + len(reply.calls) > self._limits.tool_calls:
                return self._finish(checkpoint, RunStatus.LIMIT_REACHED, artifacts)

            checkpoint, batch_artifacts = self._execute_tools(
                checkpoint, reply.calls, definitions, is_cancelled
            )
            artifacts.extend(batch_artifacts)
            seen_call_ids.update(call.call_id for call in reply.calls)

            if checkpoint.run.status in (RunStatus.WAITING, RunStatus.CANCELLED):
                return CoordinatorResult(
                    checkpoint.run,
                    artifacts=tuple(artifacts),
                    conversation_revision=checkpoint.conversation.revision,
                )

            # Saved tool results become the history for the next model request.

        return self._finish(checkpoint, RunStatus.LIMIT_REACHED, artifacts)

    def _request_model(
        self, checkpoint: Checkpoint
    ) -> tuple[ModelReply, tuple[ToolDefinition, ...]]:
        """Ask the model and retain the exact tool versions offered on this step."""

        # A permission revoked after step one must disappear from step two.
        permissions = self._permissions_for(checkpoint.run.context)
        definitions = self._registry.discover(permissions)
        model_tools = tuple(
            ModelTool(tool.name, tool.description, tool.argument_schema) for tool in definitions
        )

        # Full history includes the last batch's permitted results, e.g. {"total": 2}.
        # Future context management will select/compact history here.
        assert checkpoint.run.prompt_manifest is not None
        instructions = self._prompts.instructions(checkpoint.run.prompt_manifest)
        request = ModelRequest(
            checkpoint.conversation.items,
            model_tools,
            reasoning_effort=self._reasoning_effort,
            instructions=instructions,
            prompt_manifest=checkpoint.run.prompt_manifest,
        )
        return self._model.complete(request), definitions

    def _execute_tools(
        self,
        checkpoint: Checkpoint,
        calls: tuple[ToolCall, ...],
        definitions: tuple[ToolDefinition, ...],
        is_cancelled: Callable[[], bool] | None,
    ) -> tuple[Checkpoint, tuple[ArtifactReference, ...]]:
        """Checkpoint one batch's calls and results; return delivery metadata separately."""

        # Save intent before execution. A crash after a side effect still needs
        # future durable recovery to reconcile the missing result.
        checkpoint = self._save(
            checkpoint, RunStatus.RUNNING, calls, tool_calls=checkpoint.run.tool_calls + len(calls)
        )
        selected_tools = {tool.name: tool for tool in definitions}
        outputs: list[ConversationItem] = []
        artifacts: list[ArtifactReference] = []
        pending: list[PendingToolCall] = []

        for call in calls:
            if is_cancelled is not None and is_cancelled():
                checkpoint = self._save(
                    checkpoint, RunStatus.CANCELLED, tuple(outputs), artifacts=tuple(artifacts)
                )
                return checkpoint, tuple(artifacts)

            output = self._dispatch_tool(
                checkpoint.run.context, call, selected_tools.get(call.tool_name)
            )

            # Example: a restricted report contributes a receipt to history and
            # an artifact ID to authenticated delivery through CoordinatorResult.
            outputs.append(output.model_result)

            if output.artifact is not None:
                artifacts.append(output.artifact)

            if isinstance(output.model_result, PendingToolCall):
                pending.append(output.model_result)

        # Pending job/approval receipts pause the run. The durable layer handles
        # their completion events and returns the updated checkpoint to this loop.
        status = RunStatus.WAITING if pending else RunStatus.RUNNING
        checkpoint = self._save(
            checkpoint, status, tuple(outputs), tuple(pending), artifacts=tuple(artifacts)
        )
        return checkpoint, tuple(artifacts)

    def _dispatch_tool(
        self, context: RunContext, call: ToolCall, definition: ToolDefinition | None
    ) -> EnforcedOutput:
        """Dispatch an exposed tool through the gateway, with sanitised failures."""

        if definition is None:
            return EnforcedOutput(ToolFailure(call.call_id, ToolErrorCode.UNAVAILABLE))

        # The model names "report"; the runtime supplies the offered version,
        # e.g. report-v2. The gateway checks current access again before execution.
        try:
            return self._gateway.dispatch(call, version=definition.version, context=context)
        except Exception:
            return enforce_failure(call.call_id)

    def _save(
        self,
        checkpoint: Checkpoint,
        status: RunStatus,
        items: tuple[ConversationItem, ...] = (),
        pending: tuple[PendingToolCall, ...] = (),
        *,
        model_steps: int | None = None,
        tool_calls: int | None = None,
        artifacts: tuple[ArtifactReference, ...] = (),
        prompt_resolver: PromptResolver | None = None,
    ) -> Checkpoint:
        return self._store.save_checkpoint(
            checkpoint.run.context,
            expected_conversation_revision=checkpoint.conversation.revision,
            expected_run_revision=checkpoint.run.revision,
            status=status,
            items=items,
            pending_calls=pending,
            model_steps=model_steps,
            tool_calls=tool_calls,
            artifacts=artifacts,
            expected_recovery_id=checkpoint.conversation.recovery_id,
            expected_deletion_generation=checkpoint.conversation.deletion_generation,
            prompt_resolver=prompt_resolver,
        )

    def _finish(
        self, checkpoint: Checkpoint, status: RunStatus, artifacts: list[ArtifactReference]
    ) -> CoordinatorResult:
        checkpoint = self._save(checkpoint, status)
        return CoordinatorResult(
            checkpoint.run,
            artifacts=tuple(artifacts),
            conversation_revision=checkpoint.conversation.revision,
        )
