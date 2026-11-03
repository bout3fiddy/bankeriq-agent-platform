# Azure runtime deployment

Status: planned. Depends on recovery and scaling validation. Deployment still
requires an explicit request.

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
