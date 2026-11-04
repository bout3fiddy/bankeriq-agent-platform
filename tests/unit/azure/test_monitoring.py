"""Passive process sampling stays bounded and survives a failed observation."""

import asyncio
import json
import logging

from agent_runtime.monitoring import ProcessMonitor, Resources, resources
from agent_runtime.storage.connections import PrimaryConnections


def test_monitor_recovers_without_logging_error_payload_and_drains(caplog):
    caplog.set_level(logging.INFO, logger="bankeriq.lifecycle")

    async def scenario():
        observed = asyncio.Event()
        calls = 0

        def sample():
            nonlocal calls
            calls += 1

            if calls == 1:
                raise RuntimeError("PRIVATE_PROVIDER_CREDENTIAL")

            observed.set()
            return Resources(1024, 7, 2, 3, 4, 4, 0, 0, 0)

        async with ProcessMonitor("api", PrimaryConnections(""), interval=0.01, sample=sample):
            await asyncio.wait_for(observed.wait(), 1)

        before = calls
        await asyncio.sleep(0.03)
        assert calls == before == 2

    asyncio.run(scenario())
    rows = [json.loads(record.message) for record in caplog.records]
    assert [row["event"] for row in rows] == ["telemetry.dropped", "runtime.resources"]
    assert rows[1]["rss_bytes"] == 1024 and rows[1]["tasks"] == 3
    assert "PRIVATE_PROVIDER_CREDENTIAL" not in caplog.text
    assert "conversation" not in rows[1] and "run" not in rows[1]


def test_process_resources_distinguishes_unavailable_rss(monkeypatch):
    monkeypatch.setattr("pathlib.Path.read_text", lambda _: "1 12 0 0")

    async def sample():
        return resources(PrimaryConnections(""))

    measured = asyncio.run(sample())
    assert measured.rss_bytes is not None and measured.rss_bytes >= 12
    assert measured.tasks == 1 and measured.pool_size == 0

    def unavailable(_):
        raise FileNotFoundError

    monkeypatch.setattr("pathlib.Path.read_text", unavailable)
    assert asyncio.run(sample()).rss_bytes is None
