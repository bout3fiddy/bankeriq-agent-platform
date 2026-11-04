locals {
  # Container Apps names allow 32 characters, including the coordinator suffix.
  # Preserve existing short names. Longer prefixes get a bounded, stable hash so
  # two environments with the same leading text do not acquire the same app name.
  compute_prefix = length(var.prefix) <= 20 ? var.prefix : format(
    "%s-%s", trim(substr(var.prefix, 0, 11), "-"), substr(sha256(var.prefix), 0, 8)
  )
  common_environment = {
    AZURE_TENANT_ID      = var.tenant_id
    POSTGRES_HOST        = local.database_host
    REDIS_HOST           = azurerm_managed_redis.runtime.hostname
    ENTRA_AUDIENCE       = azuread_application.api.client_id
    RESTORE_GENERATION   = var.use_restore ? var.recovery_id : ""
    ALLOWED_USERS        = jsonencode([var.operator_id])
    INPUT_POLICY         = var.interactive ? "operator_test" : "fixtures"
    SESSION_IDLE_SECONDS = tostring(var.session_idle_seconds)
    FOUNDRY_OPENAI_BASE_URL = format(
      "https://%s.openai.azure.com/openai/v1/", var.prefix
    )
    RUNTIME_IDENTITIES = jsonencode({
      for role, identity in azurerm_user_assigned_identity.runtime : role => {
        name         = identity.name
        principal_id = identity.principal_id
      }
    })
  }
  role_environment = {
    for role, identity in azurerm_user_assigned_identity.runtime : role => merge(
      local.common_environment, {
        PILOT_TOOL_DELAY_SECONDS = role == "tool" ? tostring(var.tool_delay) : "0"
        RUNTIME_ROLE             = role
        AZURE_CLIENT_ID          = identity.client_id
        POSTGRES_USER            = identity.name
      }
    )
  }
}

resource "azurerm_container_app_job" "migration" {
  count                        = var.image == null ? 0 : 1
  name                         = "${local.compute_prefix}-migration"
  location                     = var.location
  resource_group_name          = var.resource_group
  container_app_environment_id = azurerm_container_app_environment.runtime.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 0
  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime["migration"].id]
  }
  registry {
    server   = azurerm_container_registry.runtime.login_server
    identity = azurerm_user_assigned_identity.runtime["migration"].id
  }
  template {
    container {
      name   = "migration"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"
      dynamic "env" {
        for_each = local.role_environment["migration"]
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.pull,
    azurerm_postgresql_flexible_server_active_directory_administrator.migration,
    azurerm_postgresql_flexible_server_active_directory_administrator.restored,
  azurerm_postgresql_flexible_server_database.restored]
}

resource "azurerm_container_app" "runtime" {
  for_each = (
    var.runtime_ready && var.image != null ? toset(["api", "coordinator", "tool"]) : []
  )
  name                         = "${local.compute_prefix}-${each.key}"
  resource_group_name          = var.resource_group
  container_app_environment_id = azurerm_container_app_environment.runtime.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"
  max_inactive_revisions       = 2
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime[each.key].id]
  }
  registry {
    server   = azurerm_container_registry.runtime.login_server
    identity = azurerm_user_assigned_identity.runtime[each.key].id
  }
  dynamic "ingress" {
    for_each = each.key == "api" && !var.paused ? [1] : []
    content {
      external_enabled           = true
      allow_insecure_connections = false
      target_port                = 8088
      transport                  = "http"
      traffic_weight {
        latest_revision = true
        percentage      = 100
      }
    }
  }
  template {
    min_replicas                     = var.paused ? 0 : var.replicas
    max_replicas                     = var.replicas
    termination_grace_period_seconds = 60
    container {
      name   = "runtime"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"
      dynamic "env" {
        for_each = local.role_environment[each.key]
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "readiness_probe" {
        for_each = each.key == "api" ? [1] : []
        content {
          transport = "HTTP"
          port      = 8088
          path      = "/health"
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.inference,
  azurerm_managed_redis_access_policy_assignment.runtime]
}

# A zero minimum is only a scaling hint: an existing polling worker can remain
# alive. Deactivate the paused revision explicitly before running migrations.
# For example, revision 0000005 becomes inactive; resume creates revision 0000006
# with the selected image and restores ingress. Removing this action from state
# invokes no Azure operation, so it cannot deactivate the resumed revision.
data "azapi_resource" "pause_target" {
  for_each               = var.paused ? azurerm_container_app.runtime : {}
  type                   = "Microsoft.App/containerApps@2025-07-01"
  resource_id            = each.value.id
  response_export_values = ["properties.latestRevisionName"]

  # Changing the minimum replica count can create a new revision. AzureRM once
  # planned revision 8 here and returned revision 9 during apply, invalidating
  # the action's known resource ID. Read the actual target after the app update;
  # Terraform then keeps the action target unknown until that read completes.
  depends_on = [azurerm_container_app.runtime]
}

resource "azapi_resource_action" "pause" {
  for_each = data.azapi_resource.pause_target
  type     = "Microsoft.App/containerApps/revisions@2025-07-01"
  resource_id = format(
    "%s/revisions/%s", each.value.resource_id, each.value.output.properties.latestRevisionName
  )
  action = "deactivate"
  method = "POST"
  when   = "apply"
}

output "api_url" {
  value = try(
    format("https://%s", azurerm_container_app.runtime["api"].ingress[0].fqdn), null
  )
}
