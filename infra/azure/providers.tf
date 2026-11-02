provider "azurerm" {
  subscription_id = var.subscription_id

  # Register only the service used by this configuration. Registration happens
  # through the Terraform provider, including on a fresh Azure subscription.
  resource_provider_registrations = "none"
  resource_providers_to_register  = ["Microsoft.CognitiveServices"]

  features {
    cognitive_account {
      purge_soft_delete_on_destroy = false
    }
  }
}

data "azurerm_client_config" "current" {}
