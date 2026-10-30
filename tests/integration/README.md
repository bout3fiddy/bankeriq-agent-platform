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

The fake job provider delays delivery of an already-enforced gateway result;
execution itself is synchronous. This suite does not establish production
durability, authentication, network isolation or model behaviour. Those depend on
future adapters and deployment tests. Fake approvals use in-memory decisions
bound to context, call and version; expiry and persistence are unimplemented.
