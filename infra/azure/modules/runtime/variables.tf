variable "prefix" {
  type = string
}
variable "location" {
  type = string
}
variable "resource_group" {
  type = string
}
variable "tenant_id" {
  type = string
}
variable "foundry_id" {
  type = string
}

variable "operator_id" {
  type = string
}

variable "image" {
  type    = string
  default = null
  validation {
    condition     = var.image == null ? true : can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "Hosted releases require an immutable image digest."
  }
}
variable "runtime_ready" {
  type    = bool
  default = false
}
variable "replicas" {
  type    = number
  default = 1
  validation {
    condition     = contains([1, 2], var.replicas)
    error_message = "The pilot permits one or two fixed replicas per role."
  }
}

variable "ha" {
  type    = bool
  default = false
}
variable "failover_id" {
  type    = string
  default = null
  validation {
    condition     = var.failover_id == null || var.ha
    error_message = "A planned failover requires the separately enabled HA standby."
  }
}

variable "restore_time" {
  type    = string
  default = null
  validation {
    condition     = var.restore_time == null ? true : can(formatdate("YYYY", var.restore_time))
    error_message = "Restore time must be an explicit RFC3339 timestamp."
  }
}
variable "use_restore" {
  type    = bool
  default = false
  validation {
    condition     = !var.use_restore || (var.restore_time != null && var.recovery_id != null)
    error_message = "Restored routing requires a restore point and new recovery generation."
  }
}
variable "recovery_id" {
  type    = string
  default = null
  validation {
    condition = var.recovery_id == null ? true : can(regex(
      "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.recovery_id
    ))
    error_message = "Recovery generation must be an explicit UUID."
  }
}

variable "tool_delay" {
  type    = number
  default = 0
  validation {
    condition     = var.tool_delay >= 0 && var.tool_delay <= 45
    error_message = "The synthetic drain fixture permits zero through 45 seconds."
  }
}
