# Reviewed composite business tools

Status: planned application tooling after the infrastructure groups. Depends on
asynchronous coordinator/tool queues and recovery validation. No business tools or
code-mode executor are implemented by this plan section.

Expose focused, versioned business operations through the existing tool interfaces.
Application developers define and review their composition. The Foundry model
supplies schema-validated parameters through ordinary function calling; the runtime
dispatches the operation. Internal API clients and credentials stay behind tool
implementations. A composite can call several tools asynchronously and suspend
for durable jobs or approvals without holding an execution slot.

1. **Choose a bounded business workflow** — Start with a concrete operation such as
   `compare_loan_scenarios`, `check_document_completeness` or
   `generate_portfolio_report`. Specify inputs, permitted outputs, maximum fan-out,
   partial-failure behaviour and required approvals. Keep financial calculations
   and rounding in reviewed domain implementations. Reject unsupported options;
   do not accept arbitrary scripts or model-defined execution graphs.
2. **Register and bind the composite** — Use `ToolDefinition` for its schema,
   version, permissions, approvals and output policy, and the execution interfaces
   established by the async queue group. Pin the composition and child operation
   versions for recovery. Reuse the registry, gateway and worker lifecycle rather
   than introducing another scheduler or a general-purpose shell tool.
3. **Authorise every child operation** — Dispatch nested tool requests through the
   gateway with trusted tenant/user context. Recheck resource access and approvals
   at execution and after waits; access to the composite grants no additional
   child permissions. Bind approvals to exact operation/version/arguments and
   account for each child against run budgets and trusted concurrency limits.
4. **Persist asynchronous progress** — Save the composite phase, stable child-call
   IDs, validated arguments, dependencies and permitted outcomes. Fan out
   independent children within capacity and resource-conflict limits; start
   dependent children after their inputs commit. Reuse durable joins and one
   continuation, with cancellation and partial-failure rules. Retry unfinished
   effects only under saved/current replay policy and idempotency guarantees;
   ambiguous effects require reconciliation.
5. **Preserve output boundaries** — Feed only gateway-enforced child results into
   general orchestration. Restricted processing remains inside the authorised
   business implementation; expose fixed receipts and separate authenticated
   delivery metadata. Aggregation, exceptions, progress, journals and traces must
   not expose restricted payloads or turn an artifact reference into read access.
6. **Validate with synthetic workflows** — Test bounded fan-out, dependency order,
   partial failure, duplicate completion, revoked access during a wait, approval
   expiry, cancellation and restart between children. Verify completed effects
   are not repeated and one permitted parent result reaches the model. Check
   restricted markers across model requests, persisted state and telemetry.

Example: `compare_loan_scenarios` accepts an approved scenario reference and three
rate assumptions. Its reviewed implementation obtains permitted inputs, queues
three calculation children and joins their approved comparison rows. A restart
after two results reconstructs the pending third child using its original identity.
For `generate_portfolio_report`, confidential positions stay inside the report tool;
the coordinator receives a completion receipt and the user receives protected
delivery metadata. These are proposed workflows, initially exercised with fakes.

Completion requires one composite workflow to pass local asynchronous recovery,
permission and output-isolation tests with documented limits. Adding business
operations does not itself authorise production integrations or deployment.

## Conditional follow-up: model-written code mode

Defer code mode until actual read-only workflows demonstrate a need for variable
composition beyond ordinary tool calls and reviewed composites. An `execute_code`
tool would require a sandbox plus generated `tools.*` proxies that translate script
calls into requests through the existing tool interfaces. Those proxies do not
exist today. Naming a batch tool `execute_tools` does not add script execution.

Before enabling it, require host-enforced CPU/memory/deadline/output limits,
per-child authorisation and budgets, and durable suspension/replay through the same
job lifecycle. Only externally permitted data may enter model-written scripts or
their stored state; scripts can print anything they can read. Recheck access before
reusing stored results. Disable general shell/network access and direct model-call
helpers. Validate replay compatibility, permission changes, isolation and measured
workflow benefit before deciding whether to enable this optional capability.
