output "resource_group_name" {
  value = azurerm_resource_group.runtime.name
}

output "foundry_account_id" {
  value = azurerm_cognitive_account.foundry.id
}

output "project_id" {
  value = azurerm_cognitive_account_project.runtime.id
}

output "runtime_environment" {
  description = "Non-secret configuration for the local Foundry validation command."
  value = {
    MODEL_PROVIDER          = "foundry"
    MODEL_NAME              = azurerm_cognitive_deployment.coordinator.name
    FOUNDRY_OPENAI_BASE_URL = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/openai/v1/"
    FOUNDRY_AUTH_MODE       = "azure_cli"
    AZURE_TENANT_ID         = data.azurerm_client_config.current.tenant_id
  }
}
