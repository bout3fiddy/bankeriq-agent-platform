mock_provider "azapi" {}

mock_provider "azurerm" {
  mock_resource "azurerm_virtual_network" {
    defaults = {
      id = join("", [
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
        "providers/Microsoft.Network/virtualNetworks/test"
      ])
    }
  }
}
mock_provider "azuread" {}

run "pause_keeps_resources_and_removes_dispatch_capacity" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
    image = join("", ["registry/runtime@sha256:",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    runtime_ready = true
    paused        = true
  }
  assert {
    condition = (
      length(azurerm_container_app.runtime) == 3 &&
      length(data.azapi_resource.pause_target) == 3 &&
      length(azapi_resource_action.pause) == 3 &&
      alltrue([for action in azapi_resource_action.pause : action.action == "deactivate"]) &&
      alltrue([for action in azapi_resource_action.pause : action.when == "apply"]) &&
      alltrue([for app in azurerm_container_app.runtime : app.template[0].min_replicas == 0]) &&
      length(azurerm_container_app.runtime["api"].ingress) == 0 &&
      length(azurerm_container_app_job.migration) == 1
    )
    error_message = "Pause must retain apps and migration authority while disabling ingress."
  }
}

run "restore_clone_precedes_import_and_cutover" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
    restore_time = "2026-11-03T11:32:35Z"
  }
  assert {
    condition = (
      length(azurerm_postgresql_flexible_server.restored) == 1 &&
      length(azurerm_postgresql_flexible_server_active_directory_administrator.restored) == 0 &&
      length(azurerm_postgresql_flexible_server_database.restored) == 0
    )
    error_message = "Create the clone before adopting its inherited administrator and database."
  }
}

run "private_keyless_pilot" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
  }
  assert {
    condition = (
      !azurerm_postgresql_flexible_server.runtime.public_network_access_enabled &&
      !azurerm_postgresql_flexible_server.runtime.authentication[0].password_auth_enabled &&
      azurerm_managed_redis.runtime.public_network_access == "Disabled" &&
      !azurerm_managed_redis.runtime.default_database[0].access_keys_authentication_enabled
    )
    error_message = "Data services must remain private and keyless."
  }
  assert {
    condition = (
      length(azurerm_user_assigned_identity.runtime) == 4 &&
      toset(keys(azurerm_managed_redis_access_policy_assignment.runtime)) ==
      toset(["api", "coordinator"]) &&
      azurerm_log_analytics_workspace.runtime.retention_in_days == 30 &&
      !azurerm_container_registry.runtime.admin_enabled
    )
    error_message = "Preserve role isolation, bounded logs and managed registry authentication."
  }
  assert {
    condition = (
      azuread_application.api.sign_in_audience == "AzureADMyOrg" &&
      azuread_application.api.api[0].requested_access_token_version == 2 &&
      azuread_application_pre_authorized.cli.authorized_client_id ==
      "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    )
    error_message = "Require single-tenant delegated v2 API tokens."
  }
}

run "reject_mutable_image" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
    image = "registry/runtime:latest"
  }
  expect_failures = [var.image]
}

run "reject_excessive_replicas" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
    replicas = 3
  }
  expect_failures = [var.replicas]
}

run "ha_does_not_trigger_failover" {
  command = plan
  module {
    source = "./modules/runtime"
  }
  variables {
    prefix         = "bankeriq-test"
    location       = "swedencentral"
    resource_group = "bankeriq-test"
    operator_id    = "11111111-1111-1111-1111-111111111111"
    tenant_id      = "00000000-0000-0000-0000-000000000000"
    foundry_id = join("", [
      "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/",
      "providers/Microsoft.CognitiveServices/accounts/test"
    ])
    ha = true
  }
  assert {
    condition = (
      azurerm_postgresql_flexible_server.runtime.high_availability[0].mode == "ZoneRedundant" &&
      length(azapi_resource_action.failover) == 0
    )
    error_message = "Enabling the standby must not invoke a failover automatically."
  }
}
