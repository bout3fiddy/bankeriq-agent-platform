variable "subscription_id" {
  description = "Azure subscription selected explicitly for this development environment."
  type        = string
}

variable "location" {
  description = "EU account location; confirm model availability and quota before apply."
  type        = string
  default     = "westeurope"

  validation {
    condition     = contains(["westeurope", "swedencentral"], var.location)
    error_message = "This development configuration supports West Europe or Sweden Central."
  }
}

variable "name_prefix" {
  description = "Environment prefix; a subscription-derived suffix makes the account name distinct."
  type        = string
  default     = "bankeriq-dev"
}

variable "model_name" {
  description = "Model verified for text and function Chat Completions."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "model_version" {
  description = "Explicit model version to validate."
  type        = string
  default     = "2025-04-14"
}

variable "model_capacity" {
  description = "Quota units for the deployment; capacity controls request throughput."
  type        = number
  default     = 10

  validation {
    condition = (
      var.model_capacity >= 1 && var.model_capacity <= 10 &&
      floor(var.model_capacity) == var.model_capacity
    )
    error_message = "Use 1 through 10 quota units for this bounded development deployment."
  }
}

variable "deployment_type" {
  description = "Pay-per-token processing within the EU data zone or the selected Azure geography."
  type        = string
  default     = "DataZoneStandard"

  validation {
    condition     = contains(["DataZoneStandard", "Standard"], var.deployment_type)
    error_message = "Use DataZoneStandard or regional Standard for this EU development environment."
  }
}
