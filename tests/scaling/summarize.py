"""Check retained load evidence and compare windows without rerunning the workload."""

import json
import sys
from pathlib import Path

from tests.scaling.load import distribution

source = Path(sys.argv[1])
data = json.loads(source.read_text())
samples = data["samples"]
early = samples[len(samples) // 4]
late = samples[3 * len(samples) // 4]
final = samples[-1]
warmed = samples[min(5, len(samples) - 1)]["rss_bytes"]
deltas = [a - b for a, b in zip(data["rss_after_drain"], warmed, strict=True)]
assert max(deltas) <= 64 * 1024 * 1024


def counts(sample):
    return {row["tenant_id"]: row["n"] for row in sample["completed"]}


for tenant, count in counts(final).items():
    assert counts(early)[tenant] > 0
    assert count > counts(late)[tenant]

assert "worker_eligible_scan" in json.dumps(data["claim_plans"]["after"])

for process in data["processes"].values():
    assert process.get("remaining_tasks", 0) == 0
    assert process["pool_stats"]["pool_size"] <= 4
    assert process["pool_stats"]["requests_waiting"] == 0

report = {
    "early_window_end_seconds": early["seconds"],
    "early_completed": counts(early),
    "late_window_start_seconds": late["seconds"],
    "late_window_end_seconds": final["seconds"],
    "late_completed": {k: v - counts(late)[k] for k, v in counts(final).items()},
    "rss_deltas_after_drain_bytes": deltas,
    "peak_connections": max(s["database_connections"] for s in samples),
    "peak_queue_age_seconds": max(
        q["oldest_seconds"] for s in samples for q in s["queues"].values()
    ),
    "peak_projection_lag_cursors": max(s["projection_lag_cursors"] for s in samples),
    "admission_including_commit_ms": distribution(
        [v for p in data["processes"].values() for v in p.get("admission_ms", [])]
    ),
    "worker_cpu_seconds": sum(
        p["cpu_seconds"] for k, p in data["processes"].items() if not k.startswith("api")
    ),
    "wal_bytes": data["wal_bytes"],
}
Path(sys.argv[2]).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
