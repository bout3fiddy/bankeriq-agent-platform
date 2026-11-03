# Disposable Azure runtime validation

This harness hosts the existing durable API/coordinator/tool composition with synthetic
arithmetic and restricted-report fixtures. It belongs to group 09 and is not a production
entry point. The [Terraform module](../../infra/azure/modules/runtime/README.md) owns all
cloud resources; the migration job owns application schema, prompt publication and SQL
roles. Only the coordinator receives inference permission; tools cannot call a model.

Admission verifies a delegated Entra v2 token, tenant, audience, issuer, lifetime, scope,
client and explicit user allowlist. Headers supply UUID conversation/run identifiers;
they cannot replace the signed identity. The pilot only admits the exact text
`Calculate the sum of 2 and 3.` or
`Calculate the sum of 2 and 3, then prepare a restricted report.`. Permission/output checks
still apply. The authenticated report URL is a synthetic fixture with no Blob storage.

## Release sequence

Run from the repository root using the approved EU region/model variables. Saved plans,
state and local variable files stay ignored. Authorisation for a prior disposable run
is not authorisation for an unrelated later deployment.

```sh
cp infra/azure/sweden.tfvars.example infra/azure/runtime-pilot.tfvars
# Set a fresh name_prefix and runtime_enabled=true in this ignored file.
terraform -chdir=infra/azure init -input=false
terraform -chdir=infra/azure plan -var-file=runtime-pilot.tfvars -out=runtime-pilot.tfplan
terraform -chdir=infra/azure show runtime-pilot.tfplan
# Apply only the reviewed plan within deployment authorisation.
terraform -chdir=infra/azure apply runtime-pilot.tfplan
```

Set `pilot_registry` to the registry login server from `hosted_foundation`. Publish a
Linux amd64 image, inspect its digest, then set `runtime_image` to that immutable digest.

```sh
az acr login --name "${pilot_registry%%.*}"
docker buildx build --platform linux/amd64 --load -f infra/azure/Dockerfile \
  -t "$pilot_registry/runtime:group09" .
docker push "$pilot_registry/runtime:group09"
terraform -chdir=infra/azure plan -var-file=runtime-pilot.tfvars -out=release.tfplan
terraform -chdir=infra/azure show release.tfplan
terraform -chdir=infra/azure apply release.tfplan
```

Invoke the Terraform-created migration job with `az containerapp job start`. It applies
immutable migrations, publishes the checked-in prompt catalogue, configures queue bounds
and grants runtime identities DML access without schema/prompt mutation. Entra role
creation uses Azure's `postgres` database; grants target the `bankeriq` database. Verify
execution success before setting `runtime_ready=true` and reviewing/applying the apps.

Each process owns a bounded SQL pool with renewable tokens and primary-only routing.
API/coordinator processes use Redis's Entra credential provider for refresh/re-auth.
SIGTERM stops new claims and drains accepted active work before connections close.
`runtime_replicas` selects one or two fixed replicas per role. These bounds do not
establish production capacity or autoscaler compatibility.

## Hosted checks

With the apps ready, set `pilot_url` from Terraform's `api_url` output and `pilot_audience`
from `hosted_foundation.entra_audience`. The CLI keeps its delegated token in memory.

```sh
uv run python -m tests.azure.validate --url "$pilot_url" \
  --audience "$pilot_audience" --turns 3 --output scratch/azure-validation.json
```

Add `--report` to exercise the restricted-report request and separate delivery metadata.
The checker exercises unauthenticated rejection, input rejection before acceptance,
authenticated conversation ownership, duplicate admission, completed children and turns. It
rejects any restricted fixture marker in progress views. The separately authenticated
report endpoint requires a user token. A successful run alone does not establish
multi-replica scaling, token expiry, drain, Redis loss, HA failover, restore or throttling;
these remain separate group-09 evidence gates.

## Failure and recovery rehearsals

`probe refresh` checks private DNS, SQL TLS/runtime grants, two physical SQL token
acquisitions and accelerated Redis refresh/re-auth callbacks. `probe redis-reset`
flushes this disposable cache and verifies reconstruction at the durable cursor.
Run those modules through the API app's exec channel; they are guarded test fixtures.
Natural token expiry is a separate endurance limit.

For scale-in, set `runtime_replicas=2` and `runtime_tool_delay=45`, review/apply, admit
two concurrent arithmetic requests, observe active leases, then review/apply one replica.
`tests.azure.drain` additionally sends SIGTERM to the tool container's pilot PID 1 while
it owns active work. Preserve each admitted request ledger and use `tests.azure.reconnect`
after restart to assert completion, one child attempt and unchanged duplicate admission.

Enable `runtime_ha` in its own reviewed plan. Start `tests.azure.observe` against an
already completed ledger before applying a fresh `runtime_failover_id`. Wait for the
observer's `.ready` marker, apply the provider-managed restart, verify healthy swapped
zones, then touch its `--stop` path. The observer records response counts and the longest
observed outage; reconnect verifies the original acknowledged identities.

Choose a PITR timestamp after all synthetic requests complete and save their ledger.
Do not accept new requests beyond that point during this rehearsal. Set
`runtime_restore_time` to create the clone. Azure copies its administrator and database.
Pause apps with `runtime_ready=false`, set `runtime_use_restore=true` and a fresh UUID
`runtime_recovery_id`, then import both copies into the declared restored resources
before reviewing/applying cutover. The database must be deleted before its administrator
on teardown because the role owns application objects. Run the
migration job against the clone before reopening apps. It rejects unresolved effects,
advances the timeline once and verifies the old generation is fenced. Reconnect each
ledger and rebuild Redis after cutover. Production external-effect reconciliation remains
separate from these synthetic completed operations.

Exercise throttling after restore: lower `model_capacity` to one through a reviewed
plan, invoke `tests.azure.throttle_probe provider` in the coordinator, then admit a
fixed arithmetic request. The scoped `inspect` probe checks bounded exhaustion,
unknown-effect retention, denied budget reset, idempotent control receipts and tenant
isolation. The existing worker may retain a stopped unknown outcome for operator review;
this fixture must not clear it to enable another restore. Restore capacity through
Terraform before checking a fresh successful turn. Verify the effective
`x-ratelimit-limit-tokens` on a small inference response; Azure management settings can
update before inference limits. Allow up to 15 minutes as documented in Microsoft's
[quota guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota).

After disposable validation, review and apply the destroy plan with the same variables.
Verify empty Terraform state and absence of the resource group and active Entra app.
Azure may retain platform-managed soft-deleted recovery records as documented in the
[inventory](../../infra/azure/README.md). Do not delete resources through Azure CLI.
