# Group 09 foundation review — 3 November 2026

Status: completed. Initial preparation evidence is retained below; execution updates
follow it. The [lifecycle record](hosted-lifecycle.md) contains final gates and cleanup.

Before the initial apply, the subscription had no active resource groups and the local
Terraform state was empty. CLI authentication and Graph inspection succeeded. Entra
policy permits the current user to create application registrations; an apply may
still reveal additional directory restrictions. Terraform registered the required Azure providers.
No resource-group, database, cache, registry, application or model resource had been
created at that preparation checkpoint.

Live Sweden Central inspection returned the PostgreSQL `Standard_B1ms` SKU without
an offer restriction. The model catalogue returned regional `gpt-4o` version
`2024-11-20`, retirement metadata `2027-04-14`, and 50 unused Standard quota units.
The proposed deployment reserves ten. Availability must be checked again if this
plan is applied later; successful planning alone does not prove provisioning capacity.

## Reviewed saved plan

`infra/azure/runtime-pilot.tfplan` is ignored local evidence. Its matching ignored
`runtime-pilot.tfvars` selects Sweden Central, `bankeriq09`, regional Standard inference
and `runtime_enabled=true`. The actual saved plan contains **43 creates, zero updates
and zero destroys**. It includes the five Foundry foundation resources and:

- Four scoped runtime identities, registry and image-pull grants; coordinator inference grant.
- Private VNet/subnets, PostgreSQL and Redis DNS/link resources, model private DNS,
  and two private endpoints. PostgreSQL uses its delegated subnet.
- PostgreSQL 17 B1ms, 32 GiB, seven-day backup retention, password authentication disabled.
- Managed Redis B0, key authentication disabled, private access, no pilot HA/persistence.
- Container Apps Consumption environment and 30-day/1 GiB-per-day operations workspace.
- Single-tenant Entra API registration, service principal, audience URI and CLI scope consent.

The image, API/worker Container Apps, migration job and SQL-role bootstrap are subsequent
release work. No provider provisioner, Azure CLI create command or SDK resource creation
is used. The provider configuration's service registration is the only Azure mutation
performed while planning.

## Executed commands

```sh
az account show
az group list
az ad signed-in-user show
az postgres flexible-server list-skus --location swedencentral
az cognitiveservices usage list --location swedencentral
az cognitiveservices model list --location swedencentral
terraform -chdir=infra/azure init -input=false
terraform -chdir=infra/azure fmt -check -recursive
terraform -chdir=infra/azure validate
terraform -chdir=infra/azure test
terraform -chdir=infra/azure plan -input=false \
  -var-file=runtime-pilot.tfvars -out=runtime-pilot.tfplan
terraform -chdir=infra/azure show -json runtime-pilot.tfplan
```

All six mocked Terraform plan tests passed. These verify private/keyless data services,
identity partitioning, retention, delegated token settings and existing regional limits;
they do not establish hosted networking or authentication.

The Retail Prices API returned USD list rates of $0.017/hour for Redis B0, $0.0199/hour
for PostgreSQL B1ms compute, $0.1369/GB-month for PostgreSQL storage, and $0.1666/day for
Basic Registry. These four components alone total about $36.3 per 730-hour month using
32 GiB storage and 30 days of registry billing. This is not a total deployment estimate:
private endpoints/DNS, Container Apps, logs, backup overages, traffic, model tokens,
taxes and any applicable account discounts remain additional. Rates were retrieved
from Microsoft's [Retail Prices API][prices], filtered to Sweden Central and Consumption.

## Outstanding choices and gates at preparation

At the preparation checkpoint, spending/lifetime selection was pending and paid
provisioning had not started. The user subsequently authorised disposable validation
and removal of the resources afterward. No numeric spending ceiling was supplied.
There was no login/quota failure requiring manual intervention at that checkpoint.
Actual Entra consent or subscription capacity may still require intervention during provisioning.

Hosted API/worker wiring, renewable PostgreSQL/Redis tokens, SQL role isolation,
immutable image publication, hosted drain/restore/failover and token refresh evidence
remain required. A Burstable pilot cannot establish production zone-redundant HA.
Separate production recovery objectives and an HA-capable plan are required before
claiming that gate. Group 09 remained in-progress at this checkpoint.

[prices]: https://prices.azure.com/api/retail/prices

## Application verification

`uv run python scripts/verify.py` passed all static gates and **474 tests in 56.25s**,
with zero skips, against disposable PostgreSQL 17 and Redis 7 containers. The added
JWT tests use real RSA signatures and synthetic keys; they reject issuer, audience,
tenant, user/client, scope, lifetime, malformed identity and signature violations.
Foundry credential tests cover managed-identity selection, renewable callable tokens
and closure. `git diff --check`, Terraform formatting and changed Markdown links passed.

```sh
docker run -d --name bankeriq09-test-postgres \
  -e POSTGRES_PASSWORD=synthetic -p 127.0.0.1:55439:5432 postgres:17
docker run -d --name bankeriq09-test-redis -p 127.0.0.1:56389:6379 redis:7
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55439/postgres'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56389/0'
uv run python scripts/verify.py
git diff --check
docker rm -f bankeriq09-test-postgres bankeriq09-test-redis
```

## Disposable deployment execution

The user selected disposable validation and Terraform teardown. The first saved
43-create plan was applied. Private networking/DNS, Redis, registry, Container Apps
Compute environment, identities, scoped roles, logs, Entra registration and the
Foundry deployment succeeded. The B1ms PostgreSQL create failed with `SkuNotAvailable`
(request `677c53d4-0197-43dd-ab28-20b768c9ffb5`). The advertised regional default B2s
also failed with that code (request `c37f2fd1-cb46-4afd-ac17-ff946568a636`). The failed
B1ms server disappeared; B2s entered Azure's `Dropping` state. Its SKU catalogue had
listed both without restrictions. The General Purpose retry uses a fresh name after
Azure's in-progress B2s rollback temporarily prevented reuse of the original name.

The next reviewed plan selects PostgreSQL 17 General Purpose D2ds_v5: four creates
(server, database, Entra administrator and migration job), no updates or destroys.
The Linux amd64 image was built, import-checked and published to the managed registry;
its digest is recorded in the ignored variable file and Terraform plan. No static
service password or access key is used. Live CLI delegated-token acquisition and
verification against this API registration's tenant JWKS succeeded without manual consent.

The partial deployment exposed two provider drift issues that were corrected before
retry: Azure adds the database subnet's Microsoft.Storage service endpoint, now
explicitly declared; the Entra application's audience URI is owned by the separate
identifier-URI resource, so the application resource ignores that property's drift.

```sh
terraform -chdir=infra/azure apply -input=false runtime-pilot.tfplan
az acr login --name bankeriq09415eb635images
docker buildx build --platform linux/amd64 --load -f infra/azure/Dockerfile \
  -t bankeriq09415eb635images.azurecr.io/runtime:group09 .
docker push bankeriq09415eb635images.azurecr.io/runtime:group09
terraform -chdir=infra/azure plan -input=false \
  -var-file=runtime-pilot.tfvars -out=runtime-gp.tfplan
terraform -chdir=infra/azure show -json runtime-gp.tfplan
terraform -chdir=infra/azure apply -input=false runtime-gp.tfplan
```

The latest published image digest at this checkpoint is:
`sha256:cba6d7c5d64eee7ee45aa44b11b384298f2a075764600e73eba562607e2433e4`.
The initial B1ms price figures above are historical preparation estimates; they
are not the price of the General Purpose retry or a total pilot cost.

## Migration release

General Purpose provisioning succeeded in 5m7s. The Entra administrator, application
DB and migration job were subsequently created. The first job failed its verified TLS
handshake because binary libpq's default root path did not match the container. The
pilot now passes the packaged `certifi` bundle explicitly with `sslmode=verify-full`;
no certificate validation was disabled. The updated job succeeded at 11:06 UTC:
`bankeriq09-415eb635-migration-5mlny4o`, log `migration completed`.

The replacement image digest is:
`sha256:4c548290fc4e661db2510a36f57b478d3b3fd32f24802954417cdc0a0fb93882`.
That release also adds the fixed synthetic restricted-report input and checks that
hosted turns actually complete tool children, with separate report delivery metadata.

```sh
terraform -chdir=infra/azure apply -input=false runtime-release.tfplan
az containerapp job start -g bankeriq09-runtime -n bankeriq09-415eb635-migration
az containerapp job execution list -g bankeriq09-runtime -n bankeriq09-415eb635-migration
az containerapp job logs show -g bankeriq09-runtime -n bankeriq09-415eb635-migration \
  --container migration --tail 30 --format text
```

A live authenticated GET to the public model endpoint returned HTTP 403 with the
message that public access is disabled and a private endpoint is required. The
[small result](public-network.json) records this check without the token. Private
inference remains a separate hosted-app check.

The release plan also exposed automatic PostgreSQL zone placement (`2`). Terraform
now preserves that placement across image releases instead of proposing to unset it.
Optional zone-redundant HA and an explicitly triggered provider-managed failover
rehearsal are being added; they have not yet been enabled/executed in Azure.

## Hosted release follow-up

The first coordinator startup then exposed a separate optional-dependency gap:
`azure.identity.aio.ManagedIdentityCredential` needs `aiohttp`, which was absent from
the lean image. Import-only smoke tests had not constructed the async credential.
The `azure-runtime` extra now includes `aiohttp` explicitly, and the Linux smoke check
constructs that client before publication. The replacement release recovered the
previously accepted queued work; subsequent arithmetic and restricted-report requests
completed through real private Foundry inference.

Private DNS/TLS, credential handling, multiple replicas, active drain, Redis recovery,
HA failover, PITR and throttling were subsequently exercised. The
[lifecycle record](hosted-lifecycle.md) supersedes the earlier outstanding-gate statements
in this chronological review and contains the final image digest, results and limits.
It also records Azure's 9m47s quota propagation delay and the failed requests during it.
