# Preserve the local-inference profile. The hosted pilot is explicitly enabled;
# its data services remain private and its migration identity owns schema setup.
module "runtime" {
  image                  = var.runtime_image
  runtime_ready          = var.runtime_ready
  paused                 = var.runtime_paused
  replicas               = var.runtime_replicas
  tool_delay             = var.runtime_tool_delay
  interactive            = var.runtime_interactive
  alerts_enabled         = var.runtime_alerts_enabled
  alert_action_group_ids = var.runtime_alert_action_group_ids
  session_idle_seconds   = var.runtime_session_idle_seconds
  restore_time           = var.runtime_restore_time
  use_restore            = var.runtime_use_restore
  recovery_id            = var.runtime_recovery_id
  ha                     = var.runtime_ha
  failover_id            = var.runtime_failover_id
  count                  = var.runtime_enabled ? 1 : 0
  source                 = "./modules/runtime"
  prefix                 = local.account_name
  location               = var.location
  resource_group         = azurerm_resource_group.runtime.name
  tenant_id              = data.azurerm_client_config.current.tenant_id
  operator_id            = data.azurerm_client_config.current.object_id
  foundry_id             = azurerm_cognitive_account.foundry.id
}
