# Disposable networking cleanup

Azure can create `NetworkWatcherRG` and a regional Network Watcher when Terraform
creates the subscription's first virtual network. The normal runtime must not adopt
an existing shared group. This separate root supports import and reviewed destruction
only after inspection establishes that the group was created by the disposable pilot
and contains only its regional watcher. Never use it to remove shared infrastructure.

Set the inspected subscription and region in ignored `cleanup.tfvars`. Import the
existing group and watcher, inspect a destroy plan containing exactly those two
deletions, then apply it within the pilot's teardown authorisation. Do not apply a
normal creation plan. State and local variable files remain ignored.

```sh
terraform -chdir=infra/azure/automatic-networking init
terraform -chdir=infra/azure/automatic-networking import -var-file=cleanup.tfvars \
  azurerm_resource_group.automatic "$watcher_group_id"
terraform -chdir=infra/azure/automatic-networking import -var-file=cleanup.tfvars \
  azurerm_network_watcher.automatic "$watcher_id"
terraform -chdir=infra/azure/automatic-networking plan -destroy \
  -var-file=cleanup.tfvars -out=cleanup.tfplan
terraform -chdir=infra/azure/automatic-networking show cleanup.tfplan
terraform -chdir=infra/azure/automatic-networking apply cleanup.tfplan
```

Verify empty state and group absence afterward. Container Apps' separate managed
resource group is removed by deleting its environment through the main Terraform root.

## Environment stuck during deletion

Microsoft documents a [VNet-first workaround][deletion] for an environment stuck in
`ScheduledForDelete`. Use it only after deleting the pilot's apps, jobs, data services
and private endpoints, and checking that its remaining subnets have no IP configurations
or service association links. The normal dependency graph deletes the environment first.

Stop the active Terraform apply gracefully. Set `stuck_environment_network` to the
inspected VNet's name, resource group and address space in `cleanup.tfvars`. Import it
as `azurerm_virtual_network.stuck_environment[0]` in this root. After successful import,
remove the VNet and its empty subnet addresses from the main state to transfer ownership.
Keep the state backups. Review and apply a destroy plan containing only the transferred
VNet. Then review/apply a fresh destroy plan in the main root to finish environment,
logging and group deletion. No CLI delete, portal change or Terraform provisioner is used.

[deletion]:
  https://learn.microsoft.com/en-us/azure/container-apps/troubleshooting#manually-delete-the-vnet-being-used-by-the-azure-container-apps-environment
