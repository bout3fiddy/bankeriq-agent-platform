# These rules reuse the existing workspace. Keep the default disabled until the
# resource sampler is deployed and the operator approves alert costs and routing.
locals {
  alerts = {
    queue = {
      severity = 2
      description = join(" ", [
        "Due work older than 30 seconds or an uncertain/lost worker lease.",
        "Inspect scoped work and original operation IDs before authorised recovery."
      ])
      query = <<-QUERY
        ContainerAppConsoleLogs_CL
        | where TimeGenerated > ago(5m)
        | extend e = parse_json(Log_s)
        | where (tostring(e.event) == "queue.sample" and tolong(e.queue_wait_ms) > 30000)
            or tostring(e.event) in ("worker.uncertain", "worker.lease_lost")
        | project TimeGenerated, event = tostring(e.event), run = tostring(e.run),
            job = tostring(e.job), queue = tostring(e.queue), age_ms = tolong(e.queue_wait_ms)
      QUERY
    }
    provider = {
      severity = 2
      description = join(" ", [
        "Provider errors/throttles exceed 5 percent across at least five calls, or three calls.",
        "Inspect quota and durable retries; preserve budgets and accepted run identities."
      ])
      query = <<-QUERY
        ContainerAppConsoleLogs_CL
        | where TimeGenerated > ago(5m)
        | extend e = parse_json(Log_s)
        | where tostring(e.event) == "model.request"
        | summarize calls = count(), errors = countif(toint(e.status_code) != 200
            or isnull(toint(e.status_code)))
        | where errors >= 3 or (calls >= 5 and todouble(errors) / calls >= 0.05)
      QUERY
    }
    streaming = {
      severity = 2
      description = join(" ", [
        "At least three stream overflows or backend failures in five minutes.",
        "Reconnect original runs and check client consumption; never resubmit accepted input."
      ])
      query = <<-QUERY
        ContainerAppConsoleLogs_CL
        | where TimeGenerated > ago(5m)
        | extend e = parse_json(Log_s)
        | where tostring(e.event) in ("stream.overflow", "stream.failed")
        | summarize failures = count()
        | where failures >= 3
      QUERY
    }
    telemetry = {
      severity = 1
      description = join(" ", [
        "Missing role heartbeats/queue samples for five minutes, or dropped telemetry.",
        "Inspect replicas, ingestion delay and workspace quota before judging an idle chat."
      ])
      query = <<-QUERY
        ContainerAppConsoleLogs_CL
        | where TimeGenerated > ago(5m)
        | extend e = parse_json(Log_s)
        | summarize api = countif(tostring(e.event) == "runtime.resources"
                and tostring(e.role) == "api"),
            coordinator = countif(tostring(e.event) == "runtime.resources"
                and tostring(e.role) == "coordinator"),
            tool = countif(tostring(e.event) == "runtime.resources"
                and tostring(e.role) == "tool"),
            queues = countif(tostring(e.event) == "queue.sample"),
            dropped = countif(tostring(e.event) == "telemetry.dropped")
        | where api == 0 or coordinator == 0 or tool == 0 or queues == 0 or dropped > 0
      QUERY
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "runtime" {
  for_each                = var.alerts_enabled && var.runtime_ready ? local.alerts : {}
  name                    = "${var.prefix}-${each.key}"
  resource_group_name     = var.resource_group
  location                = var.location
  scopes                  = [azurerm_log_analytics_workspace.runtime.id]
  evaluation_frequency    = "PT5M"
  window_duration         = "PT5M"
  severity                = each.value.severity
  description             = each.value.description
  enabled                 = !var.paused
  auto_mitigation_enabled = true
  skip_query_validation   = false

  criteria {
    query                   = each.value.query
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = length(var.alert_action_group_ids) == 0 ? [] : [1]
    content {
      action_groups = var.alert_action_group_ids
    }
  }
}
