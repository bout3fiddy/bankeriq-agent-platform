# Hosted two-hour soak, 4 November 2026

The functional endurance checks passed: 75 accepted turns completed correctly over
7,204.8 seconds, including 65 minutes of passive SSE/idle followed by successful recall.
[Safe observations](2026-11-04-hosted-soak.json) retain every attempt, cache observation,
summary duration and the available resource measurements. The complete resource gate remains
unqualified because this image did not expose process RSS, asyncio tasks or pool waits.

## Deployment and boundaries

- Collection: 11:26:15–13:26:20 UTC, or 12:26:15–14:26:20 Europe/Amsterdam.
- Image: `sha256:93f4cb61d246cc2d2b83751f5a390b880b09fb47bebf3dac0d2a5dfa5d073de7`.
- Source HEAD: `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes.
- Two isolated conversations under the existing Entra operator; one replica per role.
- Sweden Central, regional Standard `gpt-4o-2024-11-20`, 50,000 TPM.
- Each app had 0.5 CPU/1 GiB; PostgreSQL primary and Redis remained private.
- Actual usage: 118 calls, 458,024 input tokens and 2,334 output tokens, all reconciled.
  No unknown usage or outstanding reservation remained.

The separate soak agent prepared and supervised the runner. Its assistant usage limit was
reached near the end; the independent process completed and the root agent reconciled its
final evidence. Earlier root probes used the model while the soak was passively idle.
Deployment-wide metrics include that traffic; the two conversations' counts stayed unchanged.

## Cache, streams and memory

Both Redis views expired after their 30-minute inactivity windows while SSE remained connected.
Their durable run/event counts and model usage stayed unchanged throughout idle. The observer
never renewed activity. After idle, both recall turns completed with the correct isolated value
in 4.09 and 4.44 seconds. Each used one completed `retrieve_history` tool and two model calls;
the cache rebuilt with approximately 1,800 seconds of TTL. Final recalls also passed.

SSE recorded 14 connections, 12 planned reauthorizations and zero errors. Connected coverage
was 99.64% of the idle interval, with 93,371 received bytes. Scoped lifecycle logs reconcile
14 opened and 14 closed subscriptions; none remained after idle. Entra credentials renewed
twice without interactive sign-in. Passive subscriptions did not retain Redis activity.

The sessions published 20 and 21 summaries. All 41 measured provider summary calls took
0.751–8.700 seconds; median was 1.255 seconds. These repeated fixtures establish endurance
and recall checks, rather than the separate twenty-scenario semantic-quality gate. Mixed turn
completion ranged from 1.27 to 12.83 seconds. No comparable latency percentile is claimed.

## Resources and limits

Azure Monitor supplied 119 complete one-minute buckets inside the soak interval. Each role
held one replica. PostgreSQL connections peaked at 20 against the configured budget of 80.
Maximum CPU samples were 0.045, 0.129 and 0.023 cores for API, coordinator and tool respectively.
Maximum working sets were 285.4, 351.0 and 278.2 MiB. Comparing five-minute windows after
warmup and before completion, working sets changed by -7.7%, +4.5% and +5.0% respectively.

Working set includes memory beyond process RSS and cannot qualify the specified RSS gate.
Actual RSS, task counts and application pool waits were unavailable in revision 7. The
end-of-soak replica snapshots recorded zero restarts and unchanged creation times; absent
Azure RestartCount time series are preserved as missing measurements. Revision 8 adds passive
process/pool sampling for subsequent work; it cannot retrospectively fill this gap.

No real multi-user identity, cloud horizontal scaling, failover or production availability
claim follows from this run. Local independent-process tests supply separate isolation,
recovery, slow-consumer and capacity evidence.

## Reproduction

The executed command used a read-only copy of the operator configuration and an owner-only
output directory. The supplied budget was a reserved portion of the finite campaign; unused
capacity was released only after usage reconciliation. Choose fresh paths before another run.

```sh
PYTHONPATH=. uv run python performance/workloads/soak.py \
  --config scratch/soak-new/client.json --output scratch/soak-new/observations.json \
  --stop scratch/soak-new/STOP --tenant '<authorised-tenant-id>' \
  --user '<authorised-operator-id>' --image-digest '<verified-image-digest>' \
  --dialogue tests/soak/dialogue.json --max-turns 80 \
  --budget-calls 800 --budget-input-tokens 1500000 \
  --budget-output-tokens 140000 --reserve-calls 100
```

Executed raw evidence and commands remain in `scratch/group12-soak-20261104T110908Z/`:
`run.sh`, `observations.json`, `model-final.kql`, `model-final.json`, `metrics-final/`,
`metrics-reconciled/`, `report_soak.py` and `summary.json`. Numeric provider observations match
all 118 calls and their token totals. Azure collection used `az monitor metrics list` with
PT1M aggregation and explicit UTC start/end; partial boundary minutes were excluded. Export
uses allowlisted fields and retains no messages, answers, tool payloads or credentials.
