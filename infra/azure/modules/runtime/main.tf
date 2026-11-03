# This pilot uses separate identities and private data endpoints. Container images
# and database migrations are released after this foundation is available.
locals {
  roles = toset(["api", "coordinator", "tool", "migration"])
  # Keep instance keys independent of live resources. Imports must still work
  # after a partial teardown has removed a zone or identity from state.
  data_zones = {
    postgres = "${var.prefix}.postgres.database.azure.com"
    redis    = "privatelink.redis.azure.net"
  }
  foundry_zones = toset([
    "privatelink.openai.azure.com",
    "privatelink.cognitiveservices.azure.com",
    "privatelink.services.ai.azure.com"
  ])
}

resource "azurerm_virtual_network" "runtime" {
  name                = "${var.prefix}-network"
  location            = var.location
  resource_group_name = var.resource_group
  address_space       = ["10.90.0.0/16"]
}

resource "azurerm_subnet" "compute" {
  name                 = "compute"
  resource_group_name  = var.resource_group
  virtual_network_name = azurerm_virtual_network.runtime.name
  address_prefixes     = ["10.90.0.0/23"]
  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "database" {
  name                 = "database"
  resource_group_name  = var.resource_group
  virtual_network_name = azurerm_virtual_network.runtime.name
  address_prefixes     = ["10.90.2.0/24"]
  service_endpoints    = ["Microsoft.Storage"]
  delegation {
    name = "postgresql"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "endpoints" {
  name                 = "endpoints"
  resource_group_name  = var.resource_group
  virtual_network_name = azurerm_virtual_network.runtime.name
  address_prefixes     = ["10.90.3.0/24"]
}

resource "azurerm_private_dns_zone" "data" {
  for_each            = local.data_zones
  name                = each.value
  resource_group_name = var.resource_group
}

resource "azurerm_private_dns_zone_virtual_network_link" "data" {
  for_each              = local.data_zones
  name                  = "runtime"
  resource_group_name   = var.resource_group
  private_dns_zone_name = azurerm_private_dns_zone.data[each.key].name
  virtual_network_id    = azurerm_virtual_network.runtime.id
}

resource "azurerm_user_assigned_identity" "runtime" {
  for_each            = local.roles
  name                = "${var.prefix}-${each.key}"
  location            = var.location
  resource_group_name = var.resource_group
}

resource "azurerm_container_registry" "runtime" {
  name                = replace("${var.prefix}images", "-", "")
  location            = var.location
  resource_group_name = var.resource_group
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_role_assignment" "pull" {
  for_each             = local.roles
  scope                = azurerm_container_registry.runtime.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.runtime[each.key].principal_id
}

resource "azurerm_role_assignment" "inference" {
  scope                = var.foundry_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.runtime["coordinator"].principal_id
}

resource "azurerm_postgresql_flexible_server" "runtime" {
  # Preserve Azure's chosen zone across releases; an image update must not move
  # the primary. Managed failover can later change placement independently.
  lifecycle {
    ignore_changes = [zone, high_availability[0].standby_availability_zone]
  }
  name                = "${var.prefix}-postgres-gp"
  location            = var.location
  resource_group_name = var.resource_group
  version             = "17"
  sku_name            = "GP_Standard_D2ds_v5"
  storage_mb          = 32768
  dynamic "high_availability" {
    for_each = var.ha ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = false
  delegated_subnet_id           = azurerm_subnet.database.id
  private_dns_zone_id           = azurerm_private_dns_zone.data["postgres"].id
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = var.tenant_id
  }
  depends_on = [azurerm_private_dns_zone_virtual_network_link.data]
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "migration" {
  server_name         = azurerm_postgresql_flexible_server.runtime.name
  resource_group_name = var.resource_group
  tenant_id           = var.tenant_id
  object_id           = azurerm_user_assigned_identity.runtime["migration"].principal_id
  principal_name      = azurerm_user_assigned_identity.runtime["migration"].name
  principal_type      = "ServicePrincipal"
}

resource "azurerm_postgresql_flexible_server_database" "runtime" {
  name      = "bankeriq"
  server_id = azurerm_postgresql_flexible_server.runtime.id
  charset   = "UTF8"
  collation = "en_US.utf8"
  # Destroy the owned database before dropping its Entra administrator role.
  # Azure rejects that role's deletion while application objects still depend on it.
  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.migration]
}

resource "azurerm_managed_redis" "runtime" {
  name                      = "${var.prefix}-redis"
  location                  = var.location
  resource_group_name       = var.resource_group
  sku_name                  = "Balanced_B0"
  high_availability_enabled = false
  public_network_access     = "Disabled"
  default_database {
    access_keys_authentication_enabled = false
    client_protocol                    = "Encrypted"
    clustering_policy                  = "NoCluster"
    eviction_policy                    = "AllKeysLRU"
  }
}

resource "azurerm_managed_redis_access_policy_assignment" "runtime" {
  for_each         = toset(["api", "coordinator"])
  managed_redis_id = azurerm_managed_redis.runtime.id
  object_id        = azurerm_user_assigned_identity.runtime[each.key].principal_id
}

resource "azurerm_private_endpoint" "redis" {
  name                = "${var.prefix}-redis-private"
  location            = var.location
  resource_group_name = var.resource_group
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "redis"
    private_connection_resource_id = azurerm_managed_redis.runtime.id
    subresource_names              = ["redisEnterprise"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [azurerm_private_dns_zone.data["redis"].id]
  }
}

resource "azurerm_log_analytics_workspace" "runtime" {
  name                = "${var.prefix}-operations"
  location            = var.location
  resource_group_name = var.resource_group
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 1
}

resource "azurerm_container_app_environment" "runtime" {
  name                       = "${var.prefix}-compute"
  location                   = var.location
  resource_group_name        = var.resource_group
  infrastructure_subnet_id   = azurerm_subnet.compute.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.runtime.id
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

resource "azurerm_private_dns_zone" "foundry" {
  for_each            = local.foundry_zones
  name                = each.value
  resource_group_name = var.resource_group
}

resource "azurerm_private_dns_zone_virtual_network_link" "foundry" {
  for_each              = local.foundry_zones
  name                  = "runtime"
  resource_group_name   = var.resource_group
  private_dns_zone_name = azurerm_private_dns_zone.foundry[each.key].name
  virtual_network_id    = azurerm_virtual_network.runtime.id
}

resource "azurerm_private_endpoint" "foundry" {
  name                = "${var.prefix}-foundry-private"
  location            = var.location
  resource_group_name = var.resource_group
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "foundry"
    private_connection_resource_id = var.foundry_id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "foundry"
    private_dns_zone_ids = [for zone in azurerm_private_dns_zone.foundry : zone.id]
  }
}
