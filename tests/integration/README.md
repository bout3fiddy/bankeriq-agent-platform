# Runtime integration harness

Run the synthetic workflows from the repository root:

```sh
uv run python -m pytest tests/integration -q
uv run python scripts/verify.py
```

`harness.py` wires the real coordinator, registry, gateway, output enforcement,
in-memory state store and durable continuation handler. Its model, access,
approval, job and report providers are local fakes. All inputs and reports are
synthetic; no network calls or credentials are used.

The main scenario in `test_runtime.py` follows this sequence:

1. A scripted model requests a public calculation.
2. The real gateway validates the call and filters its result.
3. The next scripted model step uses that result to request a restricted report.
4. The gateway reaches the fake approval provider, which records a pending decision.
5. An approval event triggers the durable handler and the gateway checks current access.
6. The report runs once. The fake job provider holds its enforced result until a completion event.
7. The coordinator receives a receipt and generates its final reply. The application receives
   the artifact reference separately and reads the report through the fake delivery boundary.

The same wiring covers tool and resource denial, invalid arguments, approval
denial, permission changes after discovery and across waits, handler/filter/model
failures, cancellation, duplicate completion, out-of-order jobs, and interleaved
sessions. Delivery checks ownership, current report permission and dataset access.

Each scenario checks all captured model requests and stored conversation histories
for the restricted-payload marker and artifact IDs. The inspection serializes
actual dataclass fields, including `repr=False` payloads, so hidden representations
cannot conceal a leak from these assertions. A dedicated test verifies that inspection.

`test_foundry.py` repeats the calculation/report workflow through the real OpenAI
SDK and Foundry adapter, using an in-process HTTP transport. It inspects the
serialized API payloads for restricted content and internal references, verifies
updated tool discovery after a wait, and checks failure handling for truncated
responses, invalid arguments, rate limits and timeouts. Live Azure calls remain
untested.

`test_hosting.py` exercises Microsoft's Invocations ASGI server in process. It
covers create/start/resume requests, authenticated ownership, input admission,
fixed errors, revision conflicts and simultaneous model calls from two sessions.
Authentication uses fake tokens, and input policy and inference are synthetic.
No server is deployed and no cloud identity is verified by these tests.

`test_provider_selection.py` selects two synthetic model factories through
application configuration while retaining the Foundry host, permissions, output
enforcement and approval/job continuation. It also checks model-client cleanup
when host startup fails. These fakes establish the selection boundary; other
cloud providers still need their own adapters and live compatibility checks.

The fake job provider delays delivery of an already-enforced gateway result;
execution itself is synchronous. This suite does not establish production
durability, authentication, network isolation or model behaviour. Those depend on
future adapters and deployment tests. Fake approvals use in-memory decisions
bound to context, call and version; expiry and persistence are unimplemented.

## Durable worker service tests

`test_workers.py` uses real local PostgreSQL and Redis plus independent child
processes from `worker_process.py`. It covers recovery after process death,
reconciliation after a synthetic external effect, lease and settlement races,
unknown commits, strict recovery JSON, compatibility, budgets, waits, capacity,
restricted outputs and bounded projection maintenance. See the
[reproduction commands](../../src/agent_runtime/workers/README.md).
These tests run no real model or business-tool calls.

`test_worker_controls.py` separately validates group-06 follow-ups: safe inspection,
current access/approval checks, audited idempotent commands, rollback and unknown commits,
repeated fresh-process kills before/after preparation, crash allowance exhaustion,
independent tenant progress and orderly-drain refunds. Recovery preserves completed
outcomes, operation identity and budget usage. The tests use the same disposable
PostgreSQL/Redis configuration as `test_workers.py`.
