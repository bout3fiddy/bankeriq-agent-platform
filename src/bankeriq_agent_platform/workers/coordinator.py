"""Application wiring for one prepared model boundary and durable tool fan-out."""

import asyncio
from collections.abc import Callable

from agent_runtime.conversations.messages import Message, MessageRole
from agent_runtime.coordinator.control import RunLimits
from agent_runtime.coordinator.model import AsyncModelClient
from agent_runtime.coordinator.reply_validation import is_valid_reply
from agent_runtime.coordinator.runner import Coordinator
from agent_runtime.runs.context import RunContext
from agent_runtime.runs.state import RunStatus
from agent_runtime.tools.definitions import ToolCall, ToolDefinition
from agent_runtime.tools.gateway import ToolGateway
from agent_runtime.tools.results import PendingToolCall, WaitReason
from bankeriq_agent_platform.storage.codec import plain

from .groups import OrchestrationQueue
from .state import ChildTask, Completion, Handler, Handlers, Recovery


class QueuedCoordinator:
    """Compose existing request construction and gateway enforcement with queued steps.

    prepare -> infer -> children -> prepare uses independent durable jobs. A restart
    of infer reads its prepared checkpoint and reuses the pinned prompt. The trusted
    bind callback pins replay rules and resource conflicts for each offered version;
    model arguments cannot choose queue policies, sibling policy or worker budgets.
    """

    def __init__(
        self,
        queue: OrchestrationQueue,
        coordinator: Coordinator,
        model: AsyncModelClient,
        gateway: ToolGateway,
        *,
        limits: RunLimits,
        bind: Callable[[RunContext, ToolCall, ToolDefinition, Recovery], ChildTask],
        fail_fast: bool = False,
    ) -> None:
        self.queue = queue
        self.coordinator = coordinator
        self.model = model
        self.gateway = gateway
        self.limits = limits
        self.bind = bind
        self.fail_fast = fail_fast

    def initial(self, context: RunContext, *, budget: int = 100) -> Recovery:
        return Recovery(
            kind="model_prepare",
            operation_id=f"prepare:{context.run_id}:0",
            idempotent=True,
            run_budget=budget,
        )

    async def admit(self, context: RunContext, *, request_id: str, text: str) -> str:

        # The API receives the accepted identity only after PostgreSQL commits.
        # Its short admission transaction never waits for inference or tool I/O.
        checkpoint = await asyncio.to_thread(
            self.queue.admit,
            context,
            request_id=request_id,
            items=(Message(MessageRole.USER, text),),
            state=self.initial(context),
        )
        return checkpoint.run.context.run_id

    def handlers(self) -> Handlers:
        return Handlers(
            {
                ("model_prepare", 1): Handler(lambda c, s: True, self.prepare, idempotent=True),
                ("model_infer", 1): Handler(
                    lambda c, s: True,
                    self._async_required,
                    idempotent=True,
                    async_execute=self.infer,
                ),
                ("tool", 1): Handler(
                    lambda c, s: self.gateway.can_dispatch(
                        ToolCall(**s.arguments["call"]), version=s.arguments["version"], context=c
                    ),
                    self.tool,
                    approval_required=lambda c, s: s.arguments["requires_approval"],
                ),
            }
        )

    def prepare(self, context: RunContext, state: Recovery) -> Completion:
        checkpoint = self.queue.store.load_run(context)

        if checkpoint.run.model_steps >= self.limits.model_steps:
            return Completion(RunStatus.LIMIT_REACHED)

        next_step = checkpoint.run.model_steps + 1
        return Completion(
            RunStatus.RUNNING,
            prepare_model=True,
            next_task=Recovery(
                kind="model_infer",
                operation_id=f"model:{context.run_id}:{next_step}",
                arguments={"step": next_step},
                idempotent=True,
                run_budget=state.run_budget,
            ),
        )

    async def infer(self, context: RunContext, state: Recovery) -> Completion:
        checkpoint = await asyncio.to_thread(
            self.queue.store.load_model_step, context, state.arguments["step"]
        )
        request, definitions = await asyncio.to_thread(self.coordinator.prepare_request, checkpoint)
        reply = await self.model.complete(request)
        seen = {
            item.call_id for item in checkpoint.conversation.items if isinstance(item, ToolCall)
        }

        if not is_valid_reply(reply, seen):
            return Completion(RunStatus.FAILED)

        if reply.answer is not None:
            return Completion(RunStatus.COMPLETED, (Message(MessageRole.ASSISTANT, reply.answer),))

        if checkpoint.run.tool_calls + len(reply.calls) > self.limits.tool_calls:
            return Completion(RunStatus.LIMIT_REACHED)

        offered = {definition.name: definition for definition in definitions}

        if any(call.tool_name not in offered for call in reply.calls):
            return Completion(RunStatus.FAILED)

        children = tuple(
            self.bind(context, call, offered[call.tool_name], state) for call in reply.calls
        )

        if tuple(child.call for child in children) != reply.calls:
            raise ValueError("trusted binder changed model call identity")

        return Completion(
            RunStatus.WAITING,
            reply.calls,
            pending=tuple(
                PendingToolCall(call.call_id, WaitReason.JOB, call.call_id) for call in reply.calls
            ),
            children=children,
            failure_policy="fail_fast" if self.fail_fast else "wait_all",
            tool_calls=checkpoint.run.tool_calls + len(reply.calls),
            next_task=Recovery(
                kind="model_prepare",
                operation_id=f"prepare:{context.run_id}:{state.arguments['step']}",
                idempotent=True,
                run_budget=state.run_budget,
            ),
        )

    def tool(self, context: RunContext, state: Recovery) -> Completion:
        call = ToolCall(**state.arguments["call"])
        output = self.gateway.dispatch_durable(
            call,
            version=state.arguments["version"],
            context=context,
            operation_id=state.operation_id,
        )

        if isinstance(output.model_result, PendingToolCall):
            raise ValueError("pending tool requires a registered continuation adapter")

        return Completion(
            RunStatus.RUNNING,
            (output.model_result,),
            artifacts=(output.artifact,) if output.artifact else (),
        )

    @staticmethod
    def bind_tool(
        context: RunContext,
        call: ToolCall,
        definition: ToolDefinition,
        parent: Recovery,
    ) -> ChildTask:
        return ChildTask(
            call,
            Recovery(
                kind="tool",
                operation_id=f"tool:{context.run_id}:{call.call_id}",
                arguments={
                    "call": plain(call),
                    "version": definition.version,
                    "requires_approval": definition.requires_approval,
                },
                run_budget=parent.run_budget,
            ),
        )

    @staticmethod
    def _async_required(context: RunContext, state: Recovery) -> Completion:
        raise RuntimeError("model inference requires AsyncWorker")
