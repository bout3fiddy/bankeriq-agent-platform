# Container Apps pilot foundation

This module implements the first deployment stage for group 09. It defines the
private network, data services, identities, registry, Container Apps environment,
operational log workspace and delegated Entra application. An immutable image enables
the migration job; `runtime_ready=true` enables separate API/coordinator/tool apps.
Image publication and SQL role/schema initialisation are explicit release steps.

Container Apps is selected for the asynchronous API/coordinator/tool lifecycle.
PostgreSQL owns leases and recovery. The existing Foundry Hosted Agents adapter and
its tests remain available; this decision makes no claim about hosted-agent polling
or private-connectivity support. Foundry supplies inference through a private endpoint.

The synthetic pilot uses PostgreSQL 17 General Purpose D2ds_v5 with 32 GiB storage
and seven-day backups;
Managed Redis B0 without HA/persistence; Basic Registry; a Consumption environment;
and 30-day Log Analytics retention with a 1 GiB/day ingestion cap. Public database,
cache and model access and static authentication keys are disabled. Registry image
pulls use managed identity over its authenticated public endpoint. Only the coordinator
gets model inference access. Only API/coordinator identities receive Redis access;
the migration identity is the PostgreSQL Entra administrator.

The API registration accepts delegated v2 tokens from the single tenant. Azure CLI
is pre-authorized for `runtime.invoke`. Application authentication additionally checks
an explicit user/client allowlist. The current operator is the initial configured
user. The independently tested input gate admits two fixed synthetic arithmetic/report
requests listed in the [harness guide](../../../../tests/azure/README.md). Arbitrary
user input has no approved admission path to the pilot's external model.

## Deployment sequence

1. Apply the reviewed foundation plan through Terraform. The user authorised a
   disposable validation on 3 November 2026, followed by Terraform teardown.
2. Build/publish an immutable Linux image with `infra/azure/Dockerfile`; set
   `runtime_image` to its registry digest and apply the migration job plan.
3. Invoke the migration job and verify completion before setting `runtime_ready=true`.
4. Apply the three apps with fixed bounds: `runtime_replicas` accepts one or two
   per role. Pools reserve at most four SQL connections per process; six runtime
   processes reserve 24, below the configured database budget of 56 and API reserve 8.
5. Execute authenticated hosted lifecycle, private DNS/access, token renewal, model
   throttling and recovery checks. Record executed evidence for the selected load profile.
6. Review and apply a destroy plan with the same variables; verify empty state and
   absence of the active resource group and API registration afterward.

Local Terraform state remains for this single-developer pilot. Shared/CI state needs
a separate Terraform-managed bootstrap and migration. No shared deployment may rely
on an unprotected local state file. Provider-created resources and application data
remain distinct: the migration release configures SQL roles/schema inside PostgreSQL.

Production HA, recovery targets and backup sizing are separate from this disposable
pilot. Pilot recovery targets to test are no acknowledged data loss during a same-primary
process restart, worker recovery within 60 seconds after connectivity returns, and a
manual restore completed within 30 minutes for the stated synthetic data volume.
[Executed evidence](../../../../build-order/evidence/group09/hosted-lifecycle.md) records
measurements and limits for this synthetic profile. Optional asynchronous
read replicas remain disabled; writer, prompt and cursor-required reads use the primary.

## Sources checked on 3 November 2026

[Container Apps identity][identity] supports managed identities for runtime access.
A Microsoft maintainer [confirms][scaler] that managed identity on PostgreSQL custom
scale rules is unsupported and can drop trigger metadata. The pilot therefore exercises
fixed bounded replicas. This compatibility decision uses the documented fallback;
no hosted PostgreSQL autoscaler support is claimed. [Redis private DNS][dns] uses
`privatelink.redis.azure.net`, with application TLS connections to the normal hostname.

[identity]: https://learn.microsoft.com/en-us/azure/container-apps/managed-identity
[scaler]: https://github.com/microsoft/azure-container-apps/issues/1786
[dns]: https://learn.microsoft.com/en-us/azure/redis/private-link

## Recovery rehearsals

`runtime_ha=true` enables a zone-redundant PostgreSQL standby in a separately reviewed
plan. `runtime_failover_id` then triggers one planned failover through the AzAPI
provider. A stable ID does not repeat the action during an image release; changing
it deliberately schedules another rehearsal. No CLI restart/provisioner is used.

`runtime_restore_time` creates a separate private PITR server without changing traffic.
After pausing all apps (`runtime_ready=false`), set `runtime_use_restore=true` and a
fresh UUID in `runtime_recovery_id`. Import the copied administrator and `bankeriq`
database into their declared restored resources before applying the cutover plan.
This also gives teardown the database-before-administrator dependency required by Azure.
Run the migration job on the restored endpoint;
it rejects unresolved effects, advances the generation once and expires old leases.
Reconcile the acknowledged-request ledger against the restore point before enabling
apps again. Synthetic completed arithmetic/report jobs have no external side effects;
this rehearsal cannot supply production provider receipt reconciliation.

Production planning targets are RPO zero/RTO five minutes for managed HA, and
RPO five minutes/RTO 30 minutes for PITR with request/effect reconciliation. These are
targets to validate on the chosen production data volume, rather than achieved SLOs.
The disposable pilot initially uses one primary and seven-day same-region backups.
Read replicas remain disabled. All owned state, cursor and prompt reads use the writer.

[AzAPI resource actions][actions] keep the explicit [PostgreSQL restart/failover][restart]
inside the reviewed Terraform workflow. Azure's [backup/restore guide][restore]
describes point-in-time restore into a separate server.

[actions]:
  https://learn.microsoft.com/en-us/azure/developer/terraform/get-started-azapi-resource-action-mutation
[restart]:
  https://learn.microsoft.com/en-us/rest/api/postgresql/servers/restart?view=rest-postgresql-2024-08-01
[restore]: https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-backup-restore
