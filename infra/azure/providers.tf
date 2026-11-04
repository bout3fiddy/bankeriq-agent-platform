provider "azurerm" {
  subscription_id = var.subscription_id

  # Register only services used by the selected profile. Registration happens
  # through the Terraform provider, including on a fresh Azure subscription.
  resource_provider_registrations = "none"
  # Alert rules require Insights even though their data stays in the existing
  # OperationalInsights workspace. Provider registration survives app teardown;
  # disabling these four rules must not unregister a subscription-wide service.
  # AzureRM matches registration names case-sensitively; Azure returns this
  # particular namespace as lowercase `microsoft.insights`.
  resource_providers_to_register = concat(var.runtime_enabled ? [
    "Microsoft.CognitiveServices", "Microsoft.App", "Microsoft.ContainerRegistry",
    "Microsoft.DBforPostgreSQL", "Microsoft.Cache", "Microsoft.Network",
    "Microsoft.ManagedIdentity", "Microsoft.OperationalInsights"
    ] : ["Microsoft.CognitiveServices"],
    var.runtime_enabled && var.runtime_alerts_enabled ? ["microsoft.insights"] : []
  )

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
