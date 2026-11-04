"""Conservative primary/client clock-offset bounds for delivery measurements."""

import math
from datetime import datetime
from typing import Any


def offset_interval(
    observation: dict[str, Any], *, sent: float, received: float
) -> tuple[float, float]:
    """Keep uncertainty for network transit and the timestamp query itself.

    With client times 100/101, database time 110.4, and known server work of
    0.2 seconds before/0.3 after the query, the clock offset is [9.7, 10.2].
    Older images omit server durations and retain the wider [9.4, 10.4] bound.
    Invalid durations fail measurement setup rather than claiming a narrow bound.
    """

    remote = datetime.fromisoformat(observation["database_time"]).timestamp()
    timing = observation.get("database_clock_timing", {})
    before = timing.get("before_seconds", 0.0)
    after = timing.get("after_seconds", 0.0)

    if (
        any(type(value) not in {int, float} for value in (before, after))
        or not all(math.isfinite(value) and value >= 0 for value in (before, after))
        or not received >= sent
        or before + after > received - sent
    ):
        raise ValueError("invalid database clock timing interval")

    return remote - received + after, remote - sent - before
