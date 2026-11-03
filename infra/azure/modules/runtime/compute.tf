locals {
  common_environment = {
    AZURE_TENANT_ID    = var.tenant_id
    POSTGRES_HOST      = local.database_host
    REDIS_HOST         = azurerm_managed_redis.runtime.hostname
    ENTRA_AUDIENCE     = azuread_application.api.client_id
    RESTORE_GENERATION = var.use_restore ? var.recovery_id : ""
    ALLOWED_USERS      = jsonencode([var.operator_id])
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
  name                         = "${var.prefix}-migration"
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
  name                         = "${var.prefix}-${each.key}"
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
    for_each = each.key == "api" ? [1] : []
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
    min_replicas                     = var.replicas
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

output "api_url" {
  value = try(
    format("https://%s", azurerm_container_app.runtime["api"].ingress[0].fqdn), null
  )
}
