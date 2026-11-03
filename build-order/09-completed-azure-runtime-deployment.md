# Azure runtime deployment

Status: completed. Depends on recovery and scaling validation. Completed on 3 November
2026 with hosted lifecycle evidence and verified removal of the disposable resources.

1. **Settle hosting compatibility** — Container Apps is proposed for API/coordinator/
   tool pools, with Foundry inference. Record the decision against the earlier
   Foundry Hosted Agents target. If Foundry hosting remains required, validate its
   polling, private connectivity, async lifecycle and PostgreSQL recovery ownership.
   Keep the existing adapter/tests and distinguish each host's validation evidence.
2. **Prepare provider-managed infrastructure** — Define PostgreSQL, Redis, chosen
   compute, Registry, identities, private networking/DNS and approved monitoring in
   Terraform. Recheck EU availability, model version/quota and Python/container
   compatibility. Regional Sweden is approved for synthetic tests; old soft-deleted
   names may remain reserved.
3. **Integrate authentication and scaling** — Implement Entra user/input admission
   and scoped worker identities. Validate Container Apps' PostgreSQL scaler and
   authentication; use fixed bounded replicas if unsupported. Select appropriate
   pooling, production HA, backups and log retention separately from pilot sizing.
   Set recovery-point/time targets and writer/read connection routes explicitly;
   distinguish managed HA standby from optional asynchronous read replicas.
4. **Publish and deploy when requested** — Publish immutable images, review the
   Terraform plan, then apply within deployment authorisation. Follow the
   [Azure inventory](../infra/azure/README.md); shared Terraform state is separately
   provider-managed infrastructure.
5. **Validate the hosted lifecycle** — Test warm/cold starts, multiple replicas,
   private access, token refresh, scale-in/drain, Redis loss, PostgreSQL failover/
   restore and model throttling. Use synthetic tools and authenticated report-link
   fixtures; tool-specific Blob storage remains separate work.

Completion requires executed evidence on the chosen host and stated load profile.
Foundry inference alone does not establish Foundry Hosted Agents execution.

## Current implementation and evidence

Started on 3 November 2026 at the user's request. Container Apps is selected for the
queued pilot; the Foundry Hosted Agents adapter and its existing tests remain intact.
Terraform has deployed the private network, PostgreSQL 17 General Purpose D2ds_v5,
Managed Redis B0, registry, separate identities, logs, Entra registration, migration
job and separate API/coordinator/tool apps. The user authorised disposable validation
and removal of these resources through Terraform afterward.

Delegated Entra admission checks tenant, audience, issuer, expiry, scope, client and
user. Only two reviewed synthetic arithmetic/report inputs are admitted. The Linux
amd64 image uses managed identities and explicit verified TLS roots; migration owns
schema/prompt publication while runtime identities receive scoped application grants.

Executed hosted checks include migration completion, public inference rejection,
authenticated admission, duplicate admission, real queued tool execution, restricted
report delivery and final model completion. Arithmetic completed in 3.39s; arithmetic
plus report completed in 5.79s. The [execution review](evidence/group09/foundation-review.md)
records provisioning failures, fixes, image digests, commands and remaining gates.

The complete local quality gate passed **478 tests in 55.33s with zero skips**, using real
PostgreSQL 17/Redis 7. Ten mocked Terraform plan tests passed. Hosted checks passed:

- Private DNS/TLS, public model rejection and scoped SQL grants.
- Fresh SQL token acquisition and accelerated Redis refresh/re-auth callbacks.
- Two concurrent delayed calls on two replicas per role, scale-in and active SIGTERM drain.
- Redis loss followed by durable cursor reconstruction; worker restart preserved three
  tracked requests with one tool attempt each and unchanged duplicate admissions.
- Planned PostgreSQL HA failover: 44.12s observed read interruption, then healthy swapped
  zones; six completed runs and seven joined groups survived.
- PITR and fenced cutover: migration advanced the recovery generation, three tracked
  requests reconciled unchanged, and Redis rebuilt from the restored primary. The
  rehearsal took 12m37s, within its 30-minute target.
- Model HTTP 429 with SDK retries disabled: exactly three worker attempts, retained
  unknown outcome, denied budget reset, idempotent control receipts and tenant isolation.

After the quota change propagated to inference (confirmed after 9m47s), a fresh report
request completed on the restored database in 5.48s with both tools and separate delivery.
The two earlier post-quota failures remain recorded; their budgets were never reset.

The [lifecycle evidence](evidence/group09/hosted-lifecycle.md) contains commands, image
and provider versions, measurements, failures and JSON receipts. Terraform teardown
completed after correcting restored-database deletion order and applying Microsoft's
VNet-first workaround for a stuck environment. The [cleanup receipt](evidence/group09/cleanup.json)
confirms empty Terraform states, no active Azure resources/groups and no pilot Entra
application. Final formatting, documentation links and `git diff --check` passed.

## Remaining limits

This is a synthetic pilot with one or two fixed replicas per role and two concurrent
delayed calls. It establishes no production throughput or regional-outage SLO.
Production targets remain HA RPO zero/RTO five minutes and PITR RPO five minutes/RTO
30 minutes with external-effect reconciliation. Writer-sensitive reads use the primary;
read replicas and shared Terraform state remain separate decisions.

Container Apps is the validated host; the existing Foundry Hosted Agents adapter remains
available with its original evidence. The [Microsoft scaler review][scaler] confirms
that managed identity on the PostgreSQL custom scaler is unsupported. The pilot
exercises fixed bounded replicas;
it makes no hosted PostgreSQL autoscaler claim. Token tests
cover new SQL connections and accelerated Redis renewal callbacks, without a natural
24-hour expiry soak. Inputs are limited to reviewed synthetic fixtures; authenticated
report delivery uses a fixture endpoint. Production browser admission, tool-owned Blob
storage and broader recovery/control APIs remain separate work. A throttled exhausted
model job retains its uncertain outcome for review; the rehearsal never resets budgets.
Soft-deleted recovery records were not purged, resource-provider registrations remain
enabled, and final billing settlement was not measured.

[scaler]: https://github.com/microsoft/azure-container-apps/issues/1786
