provider "azurerm" {
  subscription_id = var.subscription_id

  # Register only services used by the selected profile. Registration happens
  # through the Terraform provider, including on a fresh Azure subscription.
  resource_provider_registrations = "none"
  resource_providers_to_register = var.runtime_enabled ? [
    "Microsoft.CognitiveServices", "Microsoft.App", "Microsoft.ContainerRegistry",
    "Microsoft.DBforPostgreSQL", "Microsoft.Cache", "Microsoft.Network",
    "Microsoft.ManagedIdentity", "Microsoft.OperationalInsights"
  ] : ["Microsoft.CognitiveServices"]

  features {
    cognitive_account {
      purge_soft_delete_on_destroy = false
    }
  }
}

data "azurerm_client_config" "current" {}

provider "azuread" {}

provider "azapi" {
  subscription_id = var.subscription_id
}
