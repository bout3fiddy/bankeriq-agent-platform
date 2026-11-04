# Hosted alert validation, 4 November 2026

All four approved alert rules fired and automatically resolved. Their delivery destination
is Azure Monitor; no action group, email or webhook was configured. The
[safe record](2026-11-04-hosted-monitoring.json) retains rule names, alert IDs and observed
transitions. The existing runtime remains running with the rules enabled.

Eight explicitly identified synthetic events were written to the coordinator's existing
console log stream at 13:42:01 UTC: one old-queue observation, three provider failures,
three stream failures and one dropped-telemetry event. All eight reached Log Analytics.
The short-lived operator helper changed no application files/configuration and made no
model requests. Fixture IDs begin with `group12-alert-validation-`; exclude them from genuine
failure-rate measurements. Actual local error-path tests remain separate evidence.

The streaming rule fired after 108 seconds; queue, provider and telemetry fired after
195–212 seconds. Every instance was explicitly observed as `Resolved` by 14:02:20 UTC
(15:02:20 Europe/Amsterdam). Azure's list API briefly omitted instances; those empty snapshots
were retained and did not count as recovery. Recorded last-modification timestamps span
13:59:48–14:01:34 UTC; the test does not treat them as exact runtime-recovery timestamps.

Each stateful rule evaluates a five-minute window every five minutes. Automatic resolution
requires three healthy evaluation periods at this frequency, as described in
[Microsoft's alert guidance][stateful]. This is an operational detection interval; it is
unrelated to conversation response time. No synthetic outage of PostgreSQL, Redis or Foundry
was induced by this validation. Portal alert instances establish the selected delivery path;
external paging remains unconfigured.

## Provisioning and reproduction

The first apply updated the three app images but failed alert creation because Insights was
unregistered. AzureRM's lookup is case-sensitive and Azure returns `microsoft.insights` in
lowercase. Terraform provider registration now uses that exact namespace when alerts are
enabled. The final reviewed plan created four rules with zero changes/deletions; subscription
registration survives application teardown. Fourteen Terraform mock tests pass and the final
live plan reports no drift. See [providers.tf](../../infra/azure/providers.tf) and the
[operator runbook](../queries/README.md) for the source configuration and queries.

Executed commands, fixture and transition watcher remain in ignored `scratch/group12/`:
`monitoring-registered-plan.log`, `monitoring-registered-apply.log`, `alerts-deployed.json`,
`exercise-alerts.py`, `alert-exercise.json`, `alert-ingestion.json`, `watch-alerts.py` and
`alert-transitions.json`. The fixture process exits after writing eight bounded JSON lines;
a 60-second alarm also limits a disconnected helper. The watcher stops on four explicit
Fired-to-Resolved transitions or after 35 one-minute observations.

Normal operator inspection does not need to inject events:

```sh
az monitor scheduled-query list --resource-group bankeriq-live-runtime -o json
alert_scope='https://management.azure.com/subscriptions/<subscription-id>'
alert_scope="$alert_scope/resourceGroups/bankeriq-live-runtime"
az rest --method get --url \
  "$alert_scope/providers/Microsoft.AlertsManagement/alerts?api-version=2019-03-01&timeRange=1d"
```

Replace the subscription placeholder with the authorised deployment's value.
Use the [Azure Monitor alerts view][portal] to inspect the
four `bankeriq-live-415eb635-*` instances. No application payload or credential enters the
tracked evidence. Raw operator responses stay local.

[stateful]: https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-overview
[portal]:
https://portal.azure.com/#view/Microsoft_Azure_Monitoring/AzureMonitoringBrowseBlade/~/alertsV2
