output "foundation" {
  description = "Non-secret endpoints and identities for the reviewed image release."
  value = {
    registry       = azurerm_container_registry.runtime.login_server
    environment_id = azurerm_container_app_environment.runtime.id
    postgres_host  = local.database_host
    redis_host     = azurerm_managed_redis.runtime.hostname
    entra_audience = azuread_application.api.client_id
    tenant_id      = var.tenant_id
    allowed_users  = [var.operator_id]
    log_workspace  = azurerm_log_analytics_workspace.runtime.workspace_id
    identities = {
      for role, identity in azurerm_user_assigned_identity.runtime : role => {
        id           = identity.id
        client_id    = identity.client_id
        principal_id = identity.principal_id
        name         = identity.name
      }
    }
  }
}
