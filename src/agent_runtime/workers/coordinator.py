"""Application wiring for one prepared model boundary and durable tool fan-out."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from time import monotonic

from agent_harness.conversations.messages import Message, MessageRole
from agent_harness.coordinator.control import RunLimits
from agent_harness.coordinator.model import (
    AsyncModelClient,
    ModelContextOverflow,
    StreamingModelClient,
)
from agent_harness.coordinator.reply_validation import is_valid_reply
from agent_harness.coordinator.runner import Coordinator
from agent_harness.memory.semantic import MemoryBlocked
from agent_harness.runs.context import RunContext
from agent_harness.runs.state import RunStatus
from agent_harness.storage.codec import fingerprint, plain
from agent_harness.tools.definitions import ToolCall, ToolDefinition
from agent_harness.tools.gateway import ToolGateway
from agent_harness.tools.results import PendingToolCall, WaitReason
from agent_runtime.memory.recovery import ContextRecovery
from agent_runtime.memory.service import SemanticMemory
from agent_runtime.sessions.publication import TextPublication
from agent_runtime.storage.history import encoded_size
from agent_runtime.telemetry import emit
from agent_runtime.workers.groups import OrchestrationQueue
from agent_runtime.workers.queue import Lease
from agent_runtime.workers.state import ChildTask, Completion, Handler, Handlers, Recovery


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
        memory: SemanticMemory,
    ) -> None:
        self.queue = queue
        self.coordinator = coordinator
        self.model = model
        self.gateway = gateway
        self.limits = limits
        self.bind = bind
        self.fail_fast = fail_fast
        self.memory = memory
        self.streaming: StreamingModelClient | None = None
        self.publication: TextPublication | None = None

    def initial(self, context: RunContext, *, budget: int = 100) -> Recovery:
        return Recovery(
            kind="model_prepare",
            operation_id=f"prepare:{context.run_id}:0",
            idempotent=True,
            run_budget=budget,
        )

    async def admit(
        self, context: RunContext, *, request_id: str, text: str, worker_budget: int = 100
    ) -> str:

        # The API receives the accepted identity only after PostgreSQL commits.
        # Its short admission transaction never waits for inference or tool I/O.
        # A caller may select a smaller existing run budget, such as eight worker
        # attempts for a short experiment. Admission pins it in the recovery state;
        # duplicate requests and children cannot replace it with a larger allowance.
        checkpoint = await asyncio.to_thread(
            self.queue.admit,
            context,
            request_id=request_id,
            items=(Message(MessageRole.USER, text),),
            state=self.initial(context, budget=worker_budget),
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
                    async_execute_with_lease=self.infer_leased,
                    required_arguments={"memory_profile": self.memory.profile},
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
                arguments={
                    "step": next_step,
                    "memory_profile": self.memory.profile,
                },
                idempotent=True,
                run_budget=state.run_budget,
            ),
        )

    async def infer_leased(
        self,
        context: RunContext,
        state: Recovery,
        lease: Lease,
    ) -> Completion:
        checkpoint = await asyncio.to_thread(
            self.queue.store.load_model_step, context, state.arguments["step"]
        )

        # Reuse the prepared step's prompt, then reconstruct historical data
        # from its fixed durable boundary. Summarizer publication checks this
        # exact lease in its transaction; an expired owner cannot publish.
        request, definitions = await asyncio.to_thread(
            self.coordinator.prepare_request, checkpoint, assemble_history=False
        )

        recovery = ContextRecovery(self.memory.storage)

        def fence(db):
            return self.queue._owned(db, lease)

        for dispatch in range(2):
            try:
                if state.arguments.get("memory_profile") != self.memory.profile:
                    raise MemoryBlocked("memory_profile_changed")

                _, policy_key = self.memory.policy(context)
                rejected = await asyncio.to_thread(recovery.read, checkpoint, policy_key)

                if rejected and rejected["retry_claimed"]:
                    raise MemoryBlocked("context_recovery_exhausted")

                assembled_at = monotonic()
                request = await self.memory.assemble(
                    checkpoint,
                    request,
                    fence=fence,
                    recovery_tokens=rejected["input_tokens"] if rejected else None,
                )
                current, definitions = await asyncio.to_thread(
                    self.coordinator.prepare_request, checkpoint, assemble_history=False
                )
                request = replace(request, tools=current.tools, instructions=current.instructions)
                self.memory.context_budget.require(request)

                if (
                    len(request.history) > self.limits.context_items
                    or encoded_size(plain([request.instructions, request.tools, request.history]))
                    > self.limits.context_bytes
                ):
                    raise MemoryBlocked("context_storage_limit")

                self.memory._current(context, policy_key)
                await asyncio.to_thread(self.memory._guard, checkpoint, fence)
                emit(
                    "memory.assembled",
                    context,
                    elapsed_ms=round((monotonic() - assembled_at) * 1000),
                    estimated_input_tokens=self.memory.context_budget.count(request),
                )

                if rejected:
                    await asyncio.to_thread(
                        recovery.claim,
                        checkpoint,
                        self.memory.context_budget.count(request),
                        fence=fence,
                    )

                if self.streaming is not None and self.publication is not None:
                    reply = await self.publication.complete(self.streaming, request, lease)
                else:
                    reply = await self.model.complete(request)

                break
            except ModelContextOverflow:
                await asyncio.to_thread(
                    recovery.reject,
                    checkpoint,
                    policy_key,
                    fingerprint(plain(request)),
                    self.memory.context_budget.count(request),
                    fence=fence,
                )

                if dispatch or rejected:
                    return self._memory_failed("provider_context_overflow_after_recovery")
            except MemoryBlocked as error:
                return self._memory_failed(str(error))
        else:
            raise AssertionError("bounded model context recovery did not terminate")

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
    def _memory_failed(reason: str) -> Completion:
        return Completion(
            RunStatus.FAILED,
            (Message(MessageRole.ASSISTANT, "Conversation memory unavailable: " + reason),),
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
