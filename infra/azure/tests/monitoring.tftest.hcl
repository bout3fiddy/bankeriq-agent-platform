mock_provider "azapi" {}
mock_provider "azuread" {}
mock_provider "azurerm" {
  mock_resource "azurerm_virtual_network" {
    defaults = {
      id = join("", [
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
        "providers/Microsoft.Network/virtualNetworks/test"
      ])
    }
  }
  mock_resource "azurerm_log_analytics_workspace" {
    override_during = plan
    defaults = {
      id = join("", [
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
        "providers/Microsoft.OperationalInsights/workspaces/test"
      ])
    }
  }
}

variables {
  prefix         = "agent-test"
  location       = "swedencentral"
  resource_group = "agent-test"
  operator_id    = "11111111-1111-1111-1111-111111111111"
  tenant_id      = "00000000-0000-0000-0000-000000000000"
  foundry_id = join("", [
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
    "providers/Microsoft.CognitiveServices/accounts/test"
  ])
  image = join("", ["registry/runtime@sha256:",
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
  runtime_ready = true
}

run "default_has_no_alert_resources" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  assert {
    condition     = length(azurerm_monitor_scheduled_query_rules_alert_v2.runtime) == 0
    error_message = "An ordinary image release must not silently enable paid alerts."
  }
}

run "approved_rules_use_existing_workspace_and_route" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    alerts_enabled = true
    alert_action_group_ids = [join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.Insights/actionGroups/approved-route"
    ])]
  }
  assert {
    condition = (
      length(azurerm_monitor_scheduled_query_rules_alert_v2.runtime) == 4 &&
      alltrue([for rule in azurerm_monitor_scheduled_query_rules_alert_v2.runtime :
        rule.enabled && rule.auto_mitigation_enabled && !rule.skip_query_validation &&
        toset(rule.action[0].action_groups) == var.alert_action_group_ids &&
        toset(rule.scopes) == toset([azurerm_log_analytics_workspace.runtime.id])
      ])
    )
    error_message = "Rules need validated queries, automatic recovery and the approved route."
  }
}

run "maintenance_disables_rules_without_deleting_them" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    alerts_enabled = true
    paused         = true
  }
  assert {
    condition = (
      length(azurerm_monitor_scheduled_query_rules_alert_v2.runtime) == 4 &&
      alltrue([for rule in azurerm_monitor_scheduled_query_rules_alert_v2.runtime : !rule.enabled])
    )
    error_message = "Maintenance must suppress expected missing-heartbeat alerts."
  }
}
