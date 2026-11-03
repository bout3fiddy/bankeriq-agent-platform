# Enable HA in a separate reviewed plan before exercising failover under load.
# A stable rehearsal ID prevents another failover during unrelated image releases.
# Changing the ID deliberately replaces the action; destroy performs no restart.
resource "terraform_data" "failover" {
  count = var.failover_id == null ? 0 : 1
  input = var.failover_id
}

resource "azapi_resource_action" "failover" {
  count       = var.failover_id == null ? 0 : 1
  type        = "Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01"
  resource_id = azurerm_postgresql_flexible_server.runtime.id
  action      = "restart"
  method      = "POST"
  body = {
    restartWithFailover = true
    failoverMode        = "PlannedFailover"
  }
  lifecycle {
    replace_triggered_by = [terraform_data.failover]
  }
}

# A restored server has a separate lifecycle and endpoint. Creating it does not
# reroute live traffic. Pause apps, rotate the generation in the migration job,
# reconcile the acknowledged-request ledger, and only then re-enable workers.
resource "azurerm_postgresql_flexible_server" "restored" {
  count                             = var.restore_time == null ? 0 : 1
  name                              = "${var.prefix}-restored"
  resource_group_name               = var.resource_group
  location                          = var.location
  create_mode                       = "PointInTimeRestore"
  source_server_id                  = azurerm_postgresql_flexible_server.runtime.id
  point_in_time_restore_time_in_utc = var.restore_time
  version                           = "17"
  sku_name                          = "GP_Standard_D2ds_v5"
  storage_mb                        = 32768
  backup_retention_days             = 7
  public_network_access_enabled     = false
  delegated_subnet_id               = azurerm_subnet.database.id
  private_dns_zone_id               = azurerm_private_dns_zone.data["postgres"].id
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = var.tenant_id
  }
  lifecycle {
    ignore_changes = [zone]
  }
}

# Azure PITR copies the administrator and application database automatically.
# Enable their declarations at cutover, then import both before planning that stage.
# The database dependency reverses during destroy: delete owned objects before
# asking Azure to drop the administrator role, which otherwise fails with 2BP01.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "restored" {
  count               = var.use_restore ? 1 : 0
  server_name         = azurerm_postgresql_flexible_server.restored[0].name
  resource_group_name = var.resource_group
  tenant_id           = var.tenant_id
  object_id           = azurerm_user_assigned_identity.runtime["migration"].principal_id
  principal_name      = azurerm_user_assigned_identity.runtime["migration"].name
  principal_type      = "ServicePrincipal"
}

resource "azurerm_postgresql_flexible_server_database" "restored" {
  count      = var.use_restore ? 1 : 0
  name       = "bankeriq"
  server_id  = azurerm_postgresql_flexible_server.restored[0].id
  charset    = "UTF8"
  collation  = "en_US.utf8"
  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.restored]
}

locals {
  database_host = var.use_restore ? (
    azurerm_postgresql_flexible_server.restored[0].fqdn
  ) : azurerm_postgresql_flexible_server.runtime.fqdn
}
