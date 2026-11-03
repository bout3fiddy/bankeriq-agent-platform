mock_provider "azapi" {}

mock_provider "azurerm" {}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000000"
}

run "eu_keyless_development" {
  command = plan

  assert {
    condition = (
      azurerm_cognitive_account.foundry.location == "westeurope" &&
      azurerm_cognitive_deployment.coordinator.sku[0].name == "DataZoneStandard" &&
      !azurerm_cognitive_account.foundry.local_auth_enabled
    )
    error_message = "The development plan must preserve EU Data Zone inference and keyless access."
  }
}

run "reject_non_eu_location" {
  command = plan

  variables {
    location = "eastus"
  }

  expect_failures = [var.location]
}

run "regional_sweden_keyless_development" {
  command = plan

  variables {
    location        = "swedencentral"
    deployment_type = "Standard"
    model_name      = "gpt-4o"
    model_version   = "2024-11-20"
  }

  assert {
    condition = (
      azurerm_cognitive_account.foundry.location == "swedencentral" &&
      azurerm_cognitive_deployment.coordinator.sku[0].name == "Standard" &&
      azurerm_cognitive_deployment.coordinator.model[0].name == "gpt-4o" &&
      !azurerm_cognitive_account.foundry.local_auth_enabled
    )
    error_message = "The regional test must preserve Sweden placement and keyless access."
  }
}

run "reject_global_deployment" {
  command = plan

  variables {
    deployment_type = "GlobalStandard"
  }

  expect_failures = [var.deployment_type]
}

run "reject_excessive_capacity" {
  command = plan

  variables {
    model_capacity = 11
  }

  expect_failures = [var.model_capacity]
}
