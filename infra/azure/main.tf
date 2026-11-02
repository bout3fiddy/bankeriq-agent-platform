locals {
  account_name = "${var.name_prefix}-${substr(sha256(var.subscription_id), 0, 8)}"
  tags = {
    project     = "bankeriq"
    environment = "development"
    managed_by  = "terraform"
  }
}

resource "azurerm_resource_group" "runtime" {
  name     = "${var.name_prefix}-runtime"
  location = var.location
  tags     = local.tags
}

resource "azurerm_cognitive_account" "foundry" {
  name                          = local.account_name
  resource_group_name           = azurerm_resource_group.runtime.name
  location                      = azurerm_resource_group.runtime.location
  kind                          = "AIServices"
  sku_name                      = "S0"
  project_management_enabled    = true
  custom_subdomain_name         = local.account_name
  local_auth_enabled            = false
  public_network_access_enabled = true
  tags                          = local.tags

  # The development machine calls the authenticated public endpoint. Network
  # isolation for hosted workloads will be designed in the hosting phase.
  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_account_project" "runtime" {
  name                 = "runtime-dev"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = azurerm_resource_group.runtime.location

  # Azure can reject simultaneous project/model writes with RequestConflict.
  # Finish model creation before adding the project on the same account.
  depends_on = [azurerm_cognitive_deployment.coordinator]

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_deployment" "coordinator" {
  name                   = "coordinator"
  cognitive_account_id   = azurerm_cognitive_account.foundry.id
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = "OpenAI"
    name    = var.model_name
    version = var.model_version
  }

  # For example, Standard in Sweden Central uses the Sweden geography quota.
  # DataZoneStandard uses a separate quota pool within the EU data zone.
  sku {
    name     = var.deployment_type
    capacity = var.model_capacity
  }
}

resource "azurerm_role_assignment" "developer_inference" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
}
