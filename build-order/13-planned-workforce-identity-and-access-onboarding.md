# Workforce identity and access onboarding

Status: planned. Added on 4 November 2026 for an internal banking-sector application. Depends
on the group-09 Entra/API boundary and group-12 session/streaming behaviour. This plan grants
no authority to provision identity resources, change a bank tenant or enrol real users.

## Purpose and selected approach

Employees sign in with their organisation's existing Microsoft Entra workforce account.
Administrators grant application access through managed assignments and application roles.
Approved first sign-in creates or links a local user profile and conversation ownership.
The bank retains authority over employee identity, MFA, device/access policy and offboarding.

Use a single-tenant workforce application for the first bank deployment. Microsoft recommends
the [single-tenant account type][account-types] for an internal organisational audience. Access
still requires explicit assignment and application permission checks; directory membership alone
does not grant runtime access. Future bank tenants require explicit trusted onboarding and
isolation review. Public customer registration, consumer/social login and External ID customer
tenants are outside this group. There is no free-tier or monthly-active-user pricing assumption.

Confirm the bank's existing licensing and procurement needs during implementation. Microsoft
documents P1/P2 licensing for [group-based application assignment][assignments] and P1 for
[Conditional Access][conditional-access]; risk-based policies require P2. Record the actual
required features, licence coverage and incremental cost before selecting optional features.
These references were reviewed on 4 November 2026 and must be rechecked before provisioning.

## Current boundary

`src/bankeriq_agent_platform/azure/authentication.py` validates delegated pilot tokens against
one tenant, audience, client allowlist, user allowlist and `runtime.invoke` scope. The current
operator uses Azure CLI authentication; there is no employee sign-in/onboarding interface.
Preserve existing operator conversations and identities when replacing pilot-only admission.
Authentication does not establish whether raw banking input is permitted for the external model.
Use synthetic/non-sensitive data until a separate input-admission policy is approved.

## Planned requirements

1. **Define the workforce access model** — Document the target tenant, client/API registrations,
   delegated scopes, application roles and explicit assignment requirement. Map approved roles
   to least-privilege runtime permissions, with separate user, approver and operator/admin
   responsibilities. Specify access-request, approval, joiner/mover/leaver and support flows.
   Existing corporate identity management owns employee creation and credential recovery.
2. **Provide browser sign-in and onboarding** — Add a minimal employee sign-in entry point,
   authenticated account/session view, sign-out and access-denied/request-access path. Use the
   supported OIDC authorisation-code flow with PKCE and validated state/nonce/redirects. Keep
   passwords and token handling inside the identity adapter/authentication boundary. Select and
   document secure browser token/session storage, CSRF protection and idle/absolute lifetimes.
   Successful authorised first sign-in provisions the local profile idempotently; users can
   open and resume multiple conversations without an Azure CLI login or Azure subscription.
3. **Verify identity before constructing runtime context** — Validate access-token signature,
   trusted issuer/JWKS, audience, time claims, tenant, client, delegated scopes and required app
   roles. Reject ID tokens used as API credentials and inappropriate app-only tokens. Key
   rotation and identity-provider outages must fail safely with bounded caches/timeouts. Build
   tenant/user context from verified immutable identifiers and a trusted mapping, never request
   fields or email addresses. Preserve the existing `tid`/`oid` identity of pilot conversations.
4. **Bind users to sessions and permissions** — Persist local account/membership status and
   conversation ownership in PostgreSQL with explicit uniqueness and migration rules. Require
   scoped access on history, run admission, SSE, retrieval, approvals, recovery controls and
   delivery references. Recheck current permissions after waits and before execution/delivery.
   Group/role claims and cached tokens have propagation delays: define authoritative policy
   lookup, cache lifetime and missing/overage-claim behaviour without broadening permissions.
5. **Enforce policy and offboarding** — Integrate with the bank's approved Conditional Access,
   MFA and device/session requirements. Define and measure revocation latency for removed
   assignments, disabled users and changed roles. Bound active SSE authorisation leases and
   close or reauthorise them on expiry/revocation. Stop new work and sensitive delivery when
   access is lost; reconcile already dispatched effects through existing recovery controls.
   Sign-out alone must not silently cancel accepted work or delete its audit trail. Preserve
   the bank's emergency access procedure and distinguish it from ordinary application access.
6. **Audit safely and manage identity infrastructure** — Record actor, role/assignment changes,
   access decisions and correlation IDs without tokens, OTPs, message bodies or unnecessary
   employee attributes. Reuse group-12 observability. Define app registrations, service
   principals, app-owned assignments and policy resources through Terraform providers under
   `infra/azure/`. Consume bank-owned directory objects as approved existing dependencies;
   document their owners. Verify provider coverage and required admin consent before changes.
   Escalate any unsupported resource or manual consent step explicitly; never silently create
   resources through a portal/CLI workaround. Keep credentials and network I/O outside core logic.
7. **Migrate the pilot safely** — Retain the operator route until its replacement passes access
   and conversation-recovery gates; document the removal condition. Test identity/schema and
   configuration rollback without losing conversations or granting access to new principals.
   Record operational ownership, licence requirements and provisioning/revocation runbooks.

## Acceptance gates

- An approved test employee signs in through the browser, receives the intended role, creates
  two conversations, signs out/in and resumes both with their history and streaming intact.
  Duplicate callbacks and concurrent first logins create one local identity. The original
  pilot user retains existing conversations without an email-based ownership migration.
- An unassigned employee, wrong-tenant user and ordinary user requesting operator/approver
  actions are denied. Forged, expired, wrong-audience/scope/client and app-only tokens fail.
  Key rotation succeeds; unavailable identity/policy dependencies cannot grant new access.
- Cross-user and cross-tenant attempts against every conversation, stream, retrieval, approval,
  recovery and delivery endpoint fail without exposing existence or restricted contents.
- Controlled role removal and account disablement meet a documented revocation bound across
  active streams, queued work, approvals and delivery. Token refresh, expired sessions, logout,
  MFA/Conditional Access challenges and provider outages have explicit tested behaviour.
- Local tests use synthetic signed tokens, fake identity transports and real PostgreSQL/Redis
  for persistence. Authorised hosted tests use separate test employees and real Entra policies.
  Bank-admin prerequisites or unavailable licences remain explicit gates; mocks cannot replace
  live federation, assignment, MFA or revocation evidence.
- Review Terraform plans, provider permissions, licence/cost assumptions and rollback. Record
  executed commands, policy versions, role mappings and safe audit evidence. Run the full uv
  quality gate with service tests enabled and `git diff --check` before completion.

## Evidence

Planning only. Workforce onboarding, the browser sign-in flow and the above access lifecycle
have not been implemented or validated. Broader banking-input policy, production business tools
and database retention remain separately owned work; record identity-record retention needs in
[group 14](14-planned-storage-lifecycle-and-capacity-controls.md).

[account-types]:
  https://learn.microsoft.com/en-us/entra/identity-platform/single-and-multi-tenant-apps
[assignments]:
  https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal
[conditional-access]:
  https://learn.microsoft.com/en-us/entra/identity/conditional-access/overview
