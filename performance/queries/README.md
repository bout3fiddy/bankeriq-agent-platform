# Operator queries

These bounded queries use the existing Container Apps Log Analytics workspace. They project
numeric counters and opaque correlation IDs. Prompts, answers, credentials and tool payloads
are outside the event interface. Read the [collection guide](../AGENTS.md) for authentication,
workspace discovery and ingestion-delay caveats.

- `concurrency.kql`: candidate ages, committed-claim upper bounds and worker durations.
- `streaming.kql`: opened/closed streams, resets, overflow and final delivery counts.
- `memory-quality.kql`: summary duration and token usage plus assembly/compaction counts.
- `failures.kql`: bounded retry, lease-loss, uncertain-effect, overflow and dropped-telemetry rows.
- `resources.kql`: current Linux process RSS, cumulative CPU/pool waits, tasks and pool bounds.

The resource query retains up to 5,000 samples across six hours. Three single-process roles
sampled every thirty seconds produce about 2,160 rows in that window. Check the first/last
sample per replica and treat a full 5,000-row response as possibly truncated; narrow the time
window or select one replica before making duration or resource-growth claims.

All five queries executed successfully against the retained Azure workspace on 4 November 2026.
The failure query returned all three attempts from the deliberately retained failed streaming
canary, correlated by run and job. This validates query ingestion/correlation. Revision 8
supplies passive process samples from all three roles. Four separately approved
Terraform alert rules are enabled in the same workspace; query files remain operator views.

```sh
operations_workspace=$(terraform -chdir=infra/azure output -json hosted_foundation | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["log_workspace"])')
az monitor log-analytics query --workspace "$operations_workspace" \
  --analytics-query "$(cat performance/queries/failures.kql)" -o json
```

## Triage

1. A provider 429 should produce bounded durable retries. Inspect the existing run and quota
   measurements; creating a new run or resetting its budget would hide the original outcome.
2. Repeated retries, uncertain effects or lost leases require scoped worker inspection. Preserve
   ownership, attempt budgets and external-effect reconciliation before any authorized recovery.
3. A stream overflow should close only that reader. Resume its original run/cursor and compare
   durable final delivery. Repeated overflow without a slow-reader test needs investigation.
4. Any dropped telemetry or unknown usage invalidates a complete performance/billing claim.
   Keep the failure and its reserved bounds in the campaign ledger.
5. For the proposed supported workload, compare eligible claim waits with p95 500 ms/max 2 s.
   Claim events cannot detect a job that is never claimed. The new process monitor also
   samples oldest due work, including jobs blocked by capacity/resources/incompatible profiles.
   It excludes future retries and inactive/deleted conversations. Due age is not eligible age.
   New `queue_wait_upper_ms` claim samples include commit acknowledgement conservatively;
   historical `queue_wait_ms` samples stop at candidate selection. The upper bound can include
   pre-eligibility ordering/resource waits. Unknown-acknowledgement recoveries remain unmeasured.

The source now supplies one passive `ProcessMonitor` per application process. It emits role
heartbeats, RSS/tasks/threads and pool counters every 30 seconds; the coordinator additionally
samples due-queue ages. It never renews conversation activity. Diagnostic exceptions produce
fixed `telemetry.dropped` events without their payload. These additions are deployed in revision
8; the earlier revision-7 soak image lacked them. Current RSS is null on platforms without Linux
`/proc`; cumulative
CPU/pool counters must be differenced within a single replica/process lifetime. Existing Azure
Monitor CPU/working-set/replica and database connection metrics are collected separately during
hosted qualification. Working set is a memory proxy; it is not process RSS.

The workspace retains logs for 30 days and has a 1 GiB/day ingestion cap. Reaching that cap can
hide later events; this resource cap is not proof of an application telemetry budget. Record
observed ingestion and the chosen future alert delivery route before enabling paging.

## Azure Monitor alerts

Four approved log-query rules were created on 4 November 2026 in the existing workspace.
They are enabled for the retained operator deployment; source defaults remain disabled. See
[alerts.tf](../../infra/azure/modules/runtime/alerts.tf). Each evaluates five minutes of
events every five minutes, automatically resolves after recovery, and disables during a
Terraform maintenance pause. Definitions and all four queries passed local/mock and live
read-only validation. Revision 8 emits the required heartbeats. Controlled firing and automatic
recovery passed for every rule, separately from the earlier query controls.

- Queue: due age over 30 seconds, uncertain work or lease loss. Preserve operation identity.
- Provider: at least three failures, or >= 5% errors across at least five requests. Includes 429.
- Streaming: at least three overflows/backend failures in five minutes. Reconnect the same run.
- Telemetry: absent API/coordinator/tool heartbeat or queue sample, or any dropped observation.

Set `runtime_alerts_enabled=true` only with deployment approval. Optional
`runtime_alert_action_group_ids` connects existing, explicitly authorised notification routes.
With no route, Azure Monitor holds alert instances for operator inspection; no email or webhook
delivery is configured. Portal delivery of all four Fired instances is verified; automatic
recovery passed for all four instances. See the
[monitoring evidence](../records/2026-11-04-hosted-monitoring.md).
Queries do not split by run IDs, avoiding unbounded alert series.
At five-minute frequency, stateful recovery requires three healthy evaluation periods.
The five-minute window tolerates ordinary ingestion lag but cannot diagnose an Azure Monitor
outage by itself. Replica metrics remain necessary when one of multiple replicas disappears.

Microsoft documents [log search rules][log-rules] and [stateful alert behaviour][alerts].
The [provider resource][terraform-alert] defines the corresponding Terraform settings.

[log-rules]:
https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-create-log-alert-rule
[alerts]: https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-overview
[terraform-alert]:
https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/monitor_scheduled_query_rules_alert_v2
