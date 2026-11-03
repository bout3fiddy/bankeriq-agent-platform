# Import-and-destroy root for isolated pilot networking. Separate state prevents
# the normal runtime from claiming a shared watcher group and permits Azure's
# VNet-first workaround when an empty environment is stuck ScheduledForDelete.
# Verify exclusive pilot ownership before importing either kind of resource.
terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "4.81.0"
    }
  }
}

variable "subscription_id" {
  type = string
}
variable "location" {
  type = string
}

variable "stuck_environment_network" {
  type = object({
    name           = string
    resource_group = string
    address_space  = list(string)
  })
  default = null
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  features {}
}

resource "azurerm_resource_group" "automatic" {
  name     = "NetworkWatcherRG"
  location = var.location
}

resource "azurerm_network_watcher" "automatic" {
  name                = "NetworkWatcher_${var.location}"
  location            = var.location
  resource_group_name = azurerm_resource_group.automatic.name
}

# This optional declaration is used only for state transfer and destruction.
# First remove every workload/data endpoint and verify no subnet IP configuration
# or service association remains. The VNet deletion also removes its empty subnet.
resource "azurerm_virtual_network" "stuck_environment" {
  count               = var.stuck_environment_network == null ? 0 : 1
  name                = var.stuck_environment_network.name
  resource_group_name = var.stuck_environment_network.resource_group
  address_space       = var.stuck_environment_network.address_space
  location            = var.location
}
