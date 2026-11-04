"""Two-hour conversation lifecycle exercise with injectable HTTP and clock boundaries."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class SoakBudget:
    """An allocated campaign share with conservative headroom for one accepted turn.

    The default reserve uses the pilot's 100 worker-attempt ceiling. Each inference
    or summary call consumes at least one of those attempts. The hosted profile
    bounds context to 8192 tokens and summary output to 1024. A lower reservation
    requires a matching trusted runtime limit before this harness is executed.
    """

    model_calls: int = 300
    input_tokens: int = 1200000
    output_tokens: int = 120000
    reserve_calls: int = 100

    def __post_init__(self) -> None:
        if not (
            1 <= self.model_calls <= 1000
            and 1 <= self.input_tokens <= 2000000
            and 1 <= self.output_tokens <= 150000
            and 1 <= self.reserve_calls <= 100
            and all(self.reserve[key] <= self.limits[key] for key in self.limits)
        ):
            raise ValueError("invalid allocated soak budget or turn reservation")

    @property
    def limits(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @property
    def reserve(self) -> dict[str, int]:
        return {
            "model_calls": self.reserve_calls,
            "input_tokens": self.reserve_calls * 8192,
            "output_tokens": self.reserve_calls * 1024,
        }


@dataclass(frozen=True)
class SoakPlan:
    duration_seconds: int = 7200
    active_seconds: int = 1200
    idle_seconds: int = 3900
    cadence_seconds: int = 45
    inspect_seconds: int = 60
    max_turns: int = 100
    min_summary_versions: int = 5
    budget: SoakBudget = field(default_factory=SoakBudget)

    def __post_init__(self) -> None:
        if not (
            7200 <= self.duration_seconds <= 14400
            and self.active_seconds >= 300
            and self.idle_seconds >= 3900
            and self.active_seconds + self.idle_seconds <= self.duration_seconds - 300
            and 10 <= self.cadence_seconds <= 300
            and 10 <= self.inspect_seconds <= 300
            and 4 <= self.max_turns <= 100
            and 5 <= self.min_summary_versions <= 50
        ):
            raise ValueError("invalid soak duration, load or acceptance bounds")


@dataclass
class Session:
    conversation: str
    run: str = field(default_factory=lambda: str(uuid4()))
    turns: int = 0


class SoakClient(Protocol):
    async def create(self) -> Session: ...

    async def turn(self, session: Session, text: str, expected: str | None) -> dict[str, Any]: ...

    async def inspect(self, session: Session) -> dict[str, Any]: ...

    async def watch(self, session: Session, observations: dict[str, Any]) -> None: ...


async def run_soak(
    client: SoakClient,
    plan: SoakPlan,
    dialogue: tuple[str, ...],
    output: Path,
    *,
    stopped: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    source_commit: str | None = None,
    image_digest: str | None = None,
    source_dirty: bool | None = None,
) -> dict[str, Any]:
    """Run two isolated canaries and preserve only reviewed numeric/status evidence.

    The first canary consumes SSE throughout idle; the second remains disconnected.
    Both must lose their Redis view, keep their durable cursor and recall their own
    verification code afterward. A synthetic clock can exercise scheduling in unit
    tests, but only a real-clock run establishes the two-hour endurance result.
    """

    if not dialogue or any(not text.strip() or len(text.encode()) > 8192 for text in dialogue):
        raise ValueError("require bounded nonempty synthetic dialogue messages")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Reserve a fresh evidence path before creating any conversations. For example,
    # rerunning with yesterday's output fails here without admitting another turn.
    with output.open("x"):
        output.chmod(0o600)

    started = clock()
    record: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "image_digest": image_digest,
        "time_source": "real_monotonic" if clock is monotonic else "injected_test_clock",
        "plan": asdict(plan),
        "reservation_per_turn": plan.budget.reserve,
        "dialogue_sha256": hashlib.sha256(json.dumps(dialogue).encode()).hexdigest(),
        "status": "running",
        "attempts": [],
        "observations": [],
        "checks": {},
        "usage": {},
        "reserved_usage": {},
        "stream": {"connections": 0, "connected_seconds": 0.0, "events": {}, "errors": 0},
        "limits": [
            "backend_resource_metrics_require_separate_collection",
            "campaign_share_must_be_allocated_without_overlap",
        ],
    }

    def save() -> None:
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(output)

    async def pause(until: float) -> None:
        while clock() < until:
            if stopped():
                raise SoakStopped("operator_stop")

            await sleep(min(30, until - clock()))

    async def inspect(sessions: list[Session], phase: str) -> list[dict[str, Any]]:
        observations = []

        for index, session in enumerate(sessions):
            observation = await client.inspect(session)
            observations.append(observation)
            record["observations"].append(
                {
                    "session": index,
                    "phase": phase,
                    "elapsed_seconds": clock() - started,
                    **observation,
                }
            )

        save()
        return observations

    previous_usage: dict[str, dict[str, int]] = {}

    async def reconcile_usage(sessions: list[Session], phase: str) -> dict[str, int]:
        """Wait briefly for asynchronous usage rows, retaining missing/unknown evidence.

        A completed model step with no matching usage observation cannot cost zero.
        After a terminal turn, allow five seconds for the bounded writer to catch up.
        Pending work, unknown token counts or a settled call-count deficit stop the
        next admission. This lower-bound check does not prove every failed call was
        observed; runtime logging-loss evidence remains a separate acceptance gate.
        """

        deadline = clock() + 5

        while True:
            try:
                observations = await asyncio.wait_for(
                    inspect(sessions, phase), timeout=max(0.01, deadline - clock())
                )
            except TimeoutError:
                observations = []

            complete = len(observations) == len(sessions)

            for session, observation in zip(sessions, observations, strict=False):
                usage = observation.get("usage")
                complete = complete and (
                    observation.get("active_run") is None
                    and usage is not None
                    and usage["unknown_usage"] == 0
                    and usage["model_calls"] >= usage["expected_calls_lower_bound"]
                    and usage["expected_calls_lower_bound"] >= session.turns
                )

            if complete:
                total = {key: 0 for key in plan.budget.limits}

                for session, observation in zip(sessions, observations, strict=True):
                    usage = observation["usage"]
                    before = previous_usage.get(session.conversation)

                    if before is not None and any(usage[key] < before[key] for key in total):
                        record["checks"]["usage_reconciled"] = False
                        raise SoakStopped("usage_counter_regressed")

                    previous_usage[session.conversation] = dict(usage)

                    for key in total:
                        total[key] += usage[key]

                record["usage"] = total
                record["checks"]["usage_reconciled"] = True
                record["checks"]["usage_within_budget"] = all(
                    total[key] <= limit for key, limit in plan.budget.limits.items()
                )
                save()

                if not record["checks"]["usage_within_budget"]:
                    raise SoakStopped("usage_budget_exceeded")

                return total

            record["checks"]["usage_reconciled"] = False
            save()

            if clock() >= deadline:
                raise SoakStopped("usage_incomplete")

            await pause(min(deadline, clock() + 0.25))

    async def turn(session: Session, index: int, text: str, expected: str | None = None) -> None:
        if stopped():
            raise SoakStopped("operator_stop")

        if len(record["attempts"]) >= plan.max_turns:
            raise SoakStopped("turn_budget_exhausted")

        before = await reconcile_usage(sessions, "before_admission")

        if any(
            before[key] + reserve > plan.budget.limits[key]
            for key, reserve in plan.budget.reserve.items()
        ):
            raise SoakStopped("usage_budget_exhausted")

        # Count attempted admission before making the request. A transport failure
        # may have committed it, so this harness never retries under a fresh ID.
        session.run = str(uuid4())
        attempt = {
            "session": index,
            "run": session.run,
            "elapsed_seconds": clock() - started,
            "status": "starting",
        }
        record["attempts"].append(attempt)
        record["reserved_usage"] = plan.budget.reserve
        record["checks"]["usage_reconciled"] = False
        save()
        attempt.update(await client.turn(session, text, expected))
        save()

        if attempt["status"] in {"completed", "failed", "cancelled", "limit_reached"}:
            if attempt["status"] == "completed":
                session.turns += 1

            after = await reconcile_usage(sessions, "after_terminal")
            attempt["usage"] = {key: after[key] - before[key] for key in before}
            record["reserved_usage"] = {}
            save()

            if any(attempt["usage"][key] > reserve for key, reserve in plan.budget.reserve.items()):
                raise SoakStopped("usage_reservation_exceeded")

        if (
            attempt["status"] != "completed"
            or attempt.get("correct") is not True
            or attempt.get("error")
        ):
            raise SoakStopped("turn_failed")

    async def active(sessions: list[Session], until: float, phase: str) -> None:
        next_turn = clock()
        next_inspection = clock()
        index = 0

        while clock() < until:
            if clock() >= next_inspection:
                await inspect(sessions, phase)
                next_inspection = clock() + plan.inspect_seconds

            if clock() >= next_turn:
                session = sessions[index % len(sessions)]
                text = dialogue[(session.turns - 1) % len(dialogue)]
                await turn(session, index % len(sessions), text)
                index += 1
                next_turn = clock() + plan.cadence_seconds

            await pause(min(until, next_turn, next_inspection))

    save()

    try:
        if stopped():
            raise SoakStopped("operator_stop")

        sessions = [await client.create(), await client.create()]
        record["conversations"] = [session.conversation for session in sessions]

        for index, session in enumerate(sessions):
            await turn(
                session,
                index,
                "Decision: the permanent verification code for this fictional session is "
                f"SOAK-CANARY-{index + 1}. Preserve it across the following planning dialogue. "
                "It is separate from the fictional project's name. Reply with exactly READY.",
                "READY",
            )

        await active(sessions, started + plan.active_seconds, "active")
        baseline = await inspect(sessions, "idle_start")

        # Start the inactivity clock only after all accepted work has finished.
        # Idle GET/SSE traffic must not create work or renew an activity deadline.
        for observation in baseline:
            cache = observation["cache"]

            if (
                observation.get("active_run") is not None
                or not cache.get("available")
                or cache.get("view_ttl_ms", -2) <= 0
                or cache["idle_timeout_seconds"] + 60 >= plan.idle_seconds
            ):
                raise SoakStopped("idle_preconditions_failed")

        idle_started = clock()
        idle_until = idle_started + plan.idle_seconds
        watcher = asyncio.create_task(client.watch(sessions[0], record["stream"]))

        try:
            while clock() < idle_until:
                await pause(min(idle_until, clock() + plan.inspect_seconds))

                if watcher.done():
                    watcher.result()
                    raise SoakStopped("idle_stream_stopped")

                await inspect(sessions, "idle")
        finally:
            watcher.cancel()

            with suppress(asyncio.CancelledError):
                await watcher

        last_idle = await inspect(sessions, "idle_end")
        record["idle_seconds"] = clock() - idle_started
        record["checks"]["cache_expired"] = all(
            item["cache"].get("available") and item["cache"].get("view_ttl_ms") == -2
            for item in last_idle
        )
        record["checks"]["durable_idle_unchanged"] = all(
            before["cursor"] == after["cursor"] and before["durable"] == after["durable"]
            for before, after in zip(baseline, last_idle, strict=True)
        )
        record["checks"]["idle_stream_coverage"] = (
            record["stream"]["connected_seconds"] >= plan.idle_seconds * 0.95
        )
        record["checks"]["idle_stream_errors"] = record["stream"]["errors"] == 0

        for index, session in enumerate(sessions):
            await turn(
                session,
                index,
                "What permanent verification code did I give for this fictional session? "
                "Retrieve earlier history if needed. Reply with that code only.",
                f"SOAK-CANARY-{index + 1}",
            )

        record["checks"]["recall_after_idle"] = True
        await active(sessions, started + plan.duration_seconds, "resumed")

        # The last turn checks the original code after the remaining compactions.
        # A correct post-idle answer alone cannot establish end-of-run memory fidelity.
        for index, session in enumerate(sessions):
            await turn(
                session,
                index,
                "What permanent verification code did I give for this fictional session? "
                "Retrieve earlier history if needed. Reply with that code only.",
                f"SOAK-CANARY-{index + 1}",
            )

        record["checks"]["recall_after_soak"] = True
        await reconcile_usage(sessions, "final_usage")
        final = await inspect(sessions, "finished")
        record["checks"]["repeated_compaction"] = all(
            item.get("summary_versions", -1) >= plan.min_summary_versions for item in final
        )

        if any(item.get("summary_versions", -1) < 0 for item in final):
            record["limits"].append("summary_publication_count_unavailable")

        record["checks"]["two_hours_elapsed"] = clock() - started >= plan.duration_seconds
        record["status"] = "passed" if all(record["checks"].values()) else "failed"
    except SoakStopped as error:
        record["status"] = "stopped"
        record["reason"] = error.args[0]
    except asyncio.CancelledError:
        record["status"] = "stopped"
        record["reason"] = "client_interrupted"
        raise
    except Exception:

        # HTTP/provider exception strings may contain endpoints or payloads. Keep
        # the failure and partial ledger, and require separate controlled diagnosis.
        record["status"] = "failed"
        record["reason"] = "observation_or_transport_failure"
    finally:
        record["elapsed_seconds"] = clock() - started
        record["ended_at"] = datetime.now(UTC).isoformat()
        save()

    return record


class SoakStopped(Exception):
    """A bounded workload or operator condition stopped further admissions."""
