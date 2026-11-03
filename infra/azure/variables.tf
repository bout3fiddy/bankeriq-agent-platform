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

variable "runtime_enabled" {
  description = "Enable the private synthetic Container Apps pilot foundation."
  type        = bool
  default     = false
}

variable "runtime_image" {
  description = "Immutable pilot image; null creates only the foundation."
  type        = string
  default     = null
}
variable "runtime_ready" {
  description = "Start application replicas after the migration job has passed."
  type        = bool
  default     = false
}
variable "runtime_replicas" {
  description = "Fixed pilot replicas per role, bounded by worker/database capacity."
  type        = number
  default     = 1
}

variable "runtime_ha" {
  type    = bool
  default = false
}
variable "runtime_failover_id" {
  type    = string
  default = null
}

variable "runtime_restore_time" {
  type    = string
  default = null
}
variable "runtime_use_restore" {
  type    = bool
  default = false
}
variable "runtime_recovery_id" {
  type    = string
  default = null
}

variable "runtime_tool_delay" {
  type    = number
  default = 0
}

variable "runtime_interactive" {
  description = "Opt in to operator-approved non-sensitive conversational test input."
  type        = bool
  default     = false
}

variable "runtime_session_idle_seconds" {
  description = "Cache inactivity window; passive SSE/status traffic does not renew it."
  type        = number
  default     = 1800
}
