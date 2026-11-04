# Revision-12 passive diagnostics, 4 November 2026

The new runtime remained bounded during its approximately 27.5-minute qualification window.
The coordinator accumulated meaningful connection-pool wait, while the sampled CPU-throttling
windows were small. A separate clock probe found substantially narrower uncertainty with warm
HTTP connections. [Numeric evidence](2026-11-04-hosted-wake-diagnostics.json) keeps the three
experiments separate. They generated no model calls or user-activity signals.

## Process resources

Each role supplied 56 samples at roughly thirty-second intervals, from 17:17 to 17:44 UTC.
The post-warmup comparison uses each role's first sample after 17:19:30 UTC, during the declared
campaign. All rows identify revision 12; counters are differenced within one process lifetime.

| Role | Maximum RSS | RSS change after warmup | Peak/final pool | Peak/final tasks | Pool wait |
| --- | ---: | ---: | ---: | ---: | ---: |
| API | 207.44 MiB | +2.92% | 4/3 | 6/3 | 16.86 s |
| Coordinator | 208.00 MiB | +1.04% | 4/3 | 12/8 | 81.56 s |
| Tool | 190.79 MiB | +0.03% | 1/1 | 4/4 | 0 s |

All sampled pool waiting counts and error counters were zero. Cumulative waits show that
transient contention occurred between samples. Wait totals include multiple overlapping
requests and cannot be divided into a latency percentile or added to client turn time.
The current connection cap is four per role. A controlled pool-capacity experiment is justified
by these waits; no larger pool or Azure resource has been configured from this observation.
This short window does not replace the earlier two-hour functional/resource records or
establish current-image endurance. Task counts are global worker/listener/monitor tasks.

## CPU quota observations

Three separate finite helpers read the existing coordinator's cgroup counters once per second
for sixty samples. Each window spans approximately 59 seconds. The configured v1 quota is
50,000 microseconds per 100,000-microsecond period, equivalent to 0.5 CPU.

| Window | Throttled periods / total | Cumulative throttle time |
| --- | ---: | ---: |
| Serial cohort | 0 / 576 | 0 ms |
| Ten-session overload | 14 / 582 | 100.00 ms |
| Five-session control | 16 / 587 | 138.40 ms |

These are container-wide counters, including the small observer process. They are short
windows and cannot rule out every burst. Their totals do not explain the approximately
one-second application overhead by themselves. No CPU/memory resource increase was applied.
Each helper exited and its private exec connection closed after its finite read-only work.

## Clock measurement uncertainty

After all inference cohorts reconciled, ten fresh HTTP clients each made five concurrent passive
inspections on completed synthetic runs, followed immediately by five inspections on their warm
connections. All 100 reads preserved activity state. Token acquisition occurred before collection.
The fifty samples per condition are diagnostics, below the qualification collection minimum.

| Connection condition | Median uncertainty width | Nearest-rank p95 width |
| --- | ---: | ---: |
| Cold | 141.93 ms | 273.46 ms |
| Warm | 44.11 ms | 50.01 ms |

Width is the conservative primary-clock interval after subtracting known server work. Connection
setup and request transit remain inside its uncertainty. The client library's default idle
keepalive expiry is five seconds, shorter than the two/five-session batch intervals. The results
support explicitly warming clock-probe connections before future timed calibration. They do
not tighten historical bounds retrospectively, prove symmetric network delay, or qualify an
unmeasured commit-to-delivery result. Existing application latency records remain unchanged.

## Executed reproduction paths

```sh
uv run python scratch/group12/collect-wake-resources.py
PYTHONPATH=. uv run python scratch/group12/probe-wake-serial-cpu.py
PYTHONPATH=. uv run python scratch/group12/run-wake-cpu-diagnostics.py
PYTHONPATH=. uv run python scratch/group12/probe-clock-connections.py
```

The scheduled scripts bind observations to named cohorts and use finite timeouts. Reproduction
requires the existing authorised operator login, inspected current image and fresh artifact
paths. Resource collection uses the tracked KQL query, filtered to revision 12. No application
payloads, SQL parameters, credentials or unrestricted command responses are retained here.
