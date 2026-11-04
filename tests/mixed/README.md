# Mixed ordinary answers and compaction

`performance.workloads.mixed_conversations` sends paired arrivals every twelve seconds:
one fresh ordinary chat returning the fixed 67-token paragraph, and one persistent memory
chat reading the reviewed disposable dialogue. This offers ten turns/minute. Five real memory
preparation turns run before timing. The default 200 pairs take about forty minutes and can
provide 100 warm one-compaction observations, depending on actual model selection. Fewer than
100 comparable observations do not qualify a percentile.

The runner labels actual zero/one/multiple summary publications separately. It retains every
scheduled arrival, including busy-lane backlog and campaign stops. A lane remains busy through
passive usage reconciliation to prevent its next turn contaminating conversation counters;
completion timing excludes those reads. Read the server's correlated summary/model events to
measure generation separately and establish actual overlap. This runner alone cannot prove
that every ordinary turn overlapped a summary, or distinguish a profile rebuild from another
multi-summary turn. Review those spans before reporting either property.

The hosted pilot permits 100 worker attempts per accepted run. Each admission therefore reserves
100 model calls, 819,200 input and 102,400 output tokens. Two overlapping runs reserve twice
that amount. Known complete usage releases unused headroom. Missing usage, uncertain admission
or unfinished work retains its reservation and stops later arrivals without replay. Budget
headroom is not expected actual consumption. The allocation must come from an explicitly
approved finite campaign; increasing a CLI argument does not authorise more traffic.

Reproduction template; the executed allocation and paths are in the record below:

```sh
uv run python -m performance.workloads.mixed_conversations \
  --config scratch/live-client.json --output scratch/new-mixed.json \
  --stop scratch/STOP-new-mixed --batches 200 \
  --tenant '<authorised-tenant-id>' --user '<authorised-operator-id>' \
  --image-digest '<inspected-deployed-image-digest>' \
  --budget-calls 1000 --budget-input-tokens 3500000 --budget-output-tokens 240000
```

The user approved a bounded group-12 campaign extension on 4 November. These example bounds
still need reservation from its remaining ledger before execution. The client config is read-only.
No new identity, infrastructure, data seed or direct database write is performed by this workload.
The JSON output contains numeric metadata and opaque correlation IDs; response text is checked
in memory and discarded. Preserve errors and unresolved reservations in all exports.

Local verification uses a synthetic client and real concurrent asyncio tasks:

```sh
uv run python -m pytest -q tests/mixed
```

It covers two overlapping lanes, actual-publication classification, finite reservation before
admission, unknown usage, unique run IDs and retained stopped arrivals. These checks are harness
validation, not executed hosted mixed-load evidence.

The [4 November hosted record](../../performance/records/2026-11-04-hosted-mixed.md) contains
the executed 200 paired batches plus four explicit continuation pairs: 406 accepted turns,
two client-backlog arrivals and 101 comparable one-compaction turns. All 516 provider calls
matched telemetry. Historical usage/reservations remain in the campaign ledger.
