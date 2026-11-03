"""Async summary generation and scoped context assembly over durable source records."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

from agent_harness.coordinator.model import AsyncModelClient, ModelRateLimited, ModelRequest
from agent_harness.memory.assembly import (
    SUMMARY_INSTRUCTIONS,
    SUMMARY_SCHEMA,
    assemble_request,
    plan_compaction,
    summary_request,
)
from agent_harness.memory.projection import HistoryPolicy
from agent_harness.memory.retirement import select_memory
from agent_harness.memory.semantic import (
    MemoryBlocked,
    TokenBudget,
    memory_message,
    summary_candidates,
)
from agent_harness.memory.window import MemoryWindow
from agent_harness.prompts.resolution import PromptResolver, PromptUnavailableError
from agent_harness.runs.context import RunContext
from agent_harness.storage.codec import fingerprint, plain
from agent_harness.storage.interface import Checkpoint
from agent_runtime.memory.storage import SemanticStore, manifest
from agent_runtime.storage.postgres import Database
from agent_runtime.telemetry import emit


class SemanticMemory:
    """Required queued context service with application-owned provider dependencies.

    Supply independently owned summarizer/model budgets and a current access reader.
    The queued inference handler passes its lease fence into each publication.
    Every source chunk is made of whole terminal turns; too-large individual turns
    stop explicitly. Source journal, approvals, results and deliveries remain intact.
    """

    def __init__(
        self,
        storage: SemanticStore,
        summarizer: AsyncModelClient,
        *,
        prompts: PromptResolver,
        summary_budget: TokenBudget,
        context_budget: TokenBudget,
        access_for: Callable[[RunContext], HistoryPolicy],
        max_chunks: int = 8,
        keep_recent_turns: int = 2,
        reserve_tokens: int | None = None,
        target_free_tokens: int | None = None,
    ) -> None:
        if not 1 <= max_chunks <= 32:
            raise ValueError("invalid semantic chunk limit")

        self.storage = storage
        self.summarizer = summarizer
        self.prompts = prompts
        self.summary_budget = summary_budget
        self.context_budget = context_budget
        self.access_for = access_for
        self.max_chunks = max_chunks
        self.keep_recent_turns = keep_recent_turns
        self.reserve_tokens = (
            context_budget.window // 5 if reserve_tokens is None else reserve_tokens
        )
        self.target_free_tokens = (
            context_budget.window * 2 // 5 if target_free_tokens is None else target_free_tokens
        )

        if not 0 <= keep_recent_turns <= 16 or not (
            0 <= self.reserve_tokens < self.target_free_tokens < context_budget.window
        ):
            raise ValueError("invalid automatic compaction settings")

    @property
    def profile(self) -> str:

        # Pin static composition and token settings on the coordinator job.
        # Changing a tokenizer/profile or the built-in summarization instructions
        # requires a new prepared step, never an unnoticed retry substitution.
        return fingerprint(
            [
                "structured-memory-v7-guarded-assertions",
                SUMMARY_INSTRUCTIONS,
                SUMMARY_SCHEMA,
                self.summary_budget.profile,
                self.summary_budget.window,
                self.summary_budget.output,
                self.context_budget.profile,
                self.context_budget.window,
                self.context_budget.output,
                self.keep_recent_turns,
                self.reserve_tokens,
                self.target_free_tokens,
                self.max_chunks,
            ]
        )

    def policy(self, context: RunContext) -> tuple[HistoryPolicy, str]:
        try:
            policy = self.access_for(context)
        except Exception:
            raise MemoryBlocked("history_access_denied") from None

        if not policy.revision:
            raise ValueError("history policy requires a revision")

        return policy, fingerprint(
            [
                "structured-memory-v6-retained-input",
                self.profile,
                policy.revision,
                sorted(policy.visible_tools),
                self.summary_budget.profile,
                self.summary_budget.window,
                self.summary_budget.output,
            ]
        )

    async def assemble(
        self,
        checkpoint: Checkpoint,
        base: ModelRequest,
        *,
        fence: Callable[[Database], object],
        optional_compaction: bool = False,
        recovery_tokens: int | None = None,
    ) -> ModelRequest:
        """Return a checked summary plus tail, or a fixed failure before model dispatch.

        Optional compaction failure returns an already-fitting full request. Required
        compaction failure propagates a bounded reason; it never sends an oversized
        request or an empty replacement. Successful chunk publication is resumable
        after restart, while failed chunks leave the prior usable summary unchanged.
        """

        context = checkpoint.run.context
        policy, key = self.policy(context)
        self.context_budget.require(replace(base, history=()))
        compacting = optional_compaction or recovery_tokens is not None

        for _ in range(self.max_chunks + 1):
            window = await asyncio.to_thread(self.storage.read, checkpoint, key)
            window = replace(
                window,
                facts=tuple(
                    fact
                    if policy.permits(fact)
                    else replace(
                        fact,
                        kind="redacted",
                        text="Source unavailable under current policy.",
                        label="fact",
                    )
                    for fact in window.facts
                ),
            )
            request = assemble_request(window, base, policy, self.context_budget.output)
            fits = (
                not window.has_more
                and self.context_budget.fits(request)
                and (
                    recovery_tokens is None or self.context_budget.count(request) < recovery_tokens
                )
            )
            free = (
                self.context_budget.window
                - self.context_budget.count(request)
                - self.context_budget.output
            )
            compacting = compacting or window.has_more or free < self.reserve_tokens

            if (
                fits
                and (not compacting or free >= self.target_free_tokens)
                and not (optional_compaction and _ == 0)
            ):
                self._current(context, key)
                await asyncio.to_thread(self._guard, checkpoint, fence)
                return request

            if _ == self.max_chunks:
                if fits:
                    self._current(context, key)
                    await asyncio.to_thread(self._guard, checkpoint, fence)
                    return request

                raise MemoryBlocked("summary_chunk_limit")

            try:
                await self._compact(window, base, policy, fence)
            except PromptUnavailableError:
                if not fits:
                    raise MemoryBlocked("summary_prompt_unavailable") from None

                self._current(context, key)
                await asyncio.to_thread(self._guard, checkpoint, fence)
                return request
            except MemoryBlocked:
                if not fits:
                    raise

                self._current(context, key)
                await asyncio.to_thread(self._guard, checkpoint, fence)
                return request

        raise AssertionError("bounded context loop did not terminate")

    async def _compact(
        self,
        window: MemoryWindow,
        base: ModelRequest,
        policy: HistoryPolicy,
        fence: Callable[[Database], object],
    ) -> None:
        pending = await asyncio.to_thread(self.storage.pending, window)
        prompt = manifest(pending["prompt_manifest"]) if pending else self.prompts.resolve()
        instructions = SUMMARY_INSTRUCTIONS + "\n" + self.prompts.instructions(prompt)
        plan = plan_compaction(
            window,
            base,
            policy,
            instructions,
            prompt,
            pending,
            context_budget=self.context_budget,
            summary_budget=self.summary_budget,
            keep_recent_turns=self.keep_recent_turns,
            target_free_tokens=self.target_free_tokens,
        )
        sources, selected = plan.sources, plan.turns
        partial, retained_cursor = plan.partial, plan.retained_cursor

        try:
            input_hash = fingerprint([plain(sources), self.summary_budget.profile])
        except ValueError:
            raise MemoryBlocked("summary_source_byte_limit") from None

        operation = await asyncio.to_thread(
            self.storage.reserve,
            window,
            target_turn=selected[-1]["admission_sequence"],
            target_cursor=max(
                (row["sequence"] for turn in selected for row in turn["events"]),
                default=window.covered_cursor,
            ),
            input_hash=input_hash,
            prompt=prompt,
            fence=fence,
            partial_turn=partial,
            retained_cursor=retained_cursor,
        )

        if operation["result_version"] is not None:
            return

        pinned = manifest(operation["prompt_manifest"])
        request = summary_request(
            sources,
            SUMMARY_INSTRUCTIONS + "\n" + self.prompts.instructions(pinned),
            pinned,
            self.summary_budget.output,
        )
        self.summary_budget.require(request)
        self._current(window.checkpoint.run.context, window.policy_key)
        await asyncio.to_thread(self._guard, window.checkpoint, fence)

        try:
            reply = await self.summarizer.complete(request)
        except ModelRateLimited:
            raise
        except Exception:
            raise MemoryBlocked("summary_provider_failed") from None

        if reply.answer is None or reply.calls:
            raise MemoryBlocked("summary_invalid")

        selection = select_memory(reply.answer, summary_candidates(sources), sources)
        facts = selection.facts

        # An oversized generated summary cannot replace the last usable version.
        # Check it against both budgets with all independently resolved instructions.
        if not self.summary_budget.fits(replace(request, history=(memory_message(facts),))):
            raise MemoryBlocked("summary_output_limit")

        if not self.context_budget.fits(replace(base, history=(memory_message(facts),))):
            raise MemoryBlocked("summary_output_limit")

        self._current(window.checkpoint.run.context, window.policy_key)
        await asyncio.to_thread(
            self.storage.publish,
            window,
            operation,
            facts,
            retirements=selection.retirements,
            fence=fence,
        )
        emit(
            "memory.compacted",
            window.checkpoint.run.context,
            estimated_input_tokens=self.summary_budget.count(request),
            estimated_context_tokens=self.context_budget.count(
                replace(base, history=(memory_message(facts),))
            ),
        )

    def _current(self, context: RunContext, key: str) -> None:
        if self.policy(context)[1] != key:
            raise MemoryBlocked("history_policy_changed")

    def _guard(self, checkpoint: Checkpoint, fence: Callable[[Database], object]) -> None:
        with self.storage.store._connect() as connection:
            self.storage.guard(connection, checkpoint)
            fence(connection)
