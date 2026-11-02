# Executable development runtime

This profile assembles the real coordinator, registry, gateway, output policy,
in-memory store, continuation handler and Foundry HTTP host through `open_runtime()`.
Tools and job completion are synthetic. The model can be scripted or live Foundry.

## Local validation

From the repository root:

```sh
uv sync --locked
uv run bankeriq-runtime validate
```

The command uses a temporary test token and invokes the real HTTP handlers in
process. It expects four model steps, three successive tools, one job resumption
and a final answer containing `42`. Failure returns a nonzero exit code. Routine
tests never call Azure.

Save the observed conversation with `--transcript`:

```sh
mkdir -p scratch/transcripts
uv run bankeriq-runtime validate --transcript scratch/transcripts/mock.md
```

The Markdown transcript includes the supplied instructions, exact user and
assistant text, tool arguments, permitted results, restricted receipts and runtime
wait events. Its execution trace groups actions by model call, shows each new
input carried forward, and explains pauses and completion receipts. Recorded JSON
fields and correlation IDs remain in an expandable section. Per-step timing and
token usage are not captured. It includes the final run status and call counts. The parent folder
must exist; existing files are preserved and cause the command to fail before
inference. `scratch/` is ignored by Git. Transcript export applies to `validate`.

Only recorded conversation fields are exported; credentials, internal job
references, report bodies and artifact locations stay outside the transcript.
A workflow failure still exports its safe recorded history. The output-isolation
check runs before export and blocks it if restricted content entered history.
Early startup/HTTP failures can leave an empty file.

The fixed test instructions prescribe the tool sequence. Use this transcript to
inspect execution of that workflow; broader task-planning quality needs additional
evaluation prompts. Full transcripts from earlier runs cannot be recovered after
their in-memory processes exit.

The synthetic workflow is:

```text
prompt -> sample_values -> [12, 30]
       -> calculate -> {total: 42}
       -> build_report -> waiting
       -> resume job -> restricted receipt
       -> model -> final answer
```

The report tool creates an opaque demonstration artifact reference. Report
storage and delivery remain mocked. Restricted content and artifact references
are checked against the stored conversation. Existing adapter tests inspect
the serialized model requests through a local HTTP transport.

## Start a local HTTP server

Set `BANKERIQ_DEMO_TOKEN` to a private random value, then run:

```sh
uv run bankeriq-runtime serve
```

The server binds to `127.0.0.1:8088`; `--port` selects another local port.
`--host 0.0.0.0` explicitly enables container port forwarding.
`GET /readiness` reports readiness. `POST /invocations` requires
`Authorization: Bearer <BANKERIQ_DEMO_TOKEN>`. See the
[request examples](../../agent_runtime/hosting/README.md) for create/start/resume.

This profile admits exactly this synthetic prompt:

```text
Load the synthetic sample, calculate its total using tools, then prepare a report and return the total.
```

Authentication maps the test token to a fixed demo user. Other messages fail
input admission before model access. This is an executable development profile;
enterprise identity, free-form input policy and production hosting remain pending.
The startup integration test launches a separate server process, checks denied
requests and two complete turns, then verifies graceful shutdown.

## Container validation

The root Dockerfile packages this synthetic profile using Python 3.14, digest-pinned
base images and `uv.lock`. The build installs the `foundry-hosting` and `demo`
extras; development tools and the live model extra are excluded. The final image
runs the installed command directly as UID/GID 10001. The build context allowlist
includes only package inputs, keeping local Terraform state and credentials out.

From the repository root with Docker running:

```sh
docker build -t bankeriq-runtime:synthetic .
uv run python scripts/verify_container.py
```

The verifier resolves the local image ID, runs `validate` with networking disabled,
checks startup rejection without a token, then starts a temporary HTTP container.
It checks Docker readiness, UID 10001, HTTP 401/403 boundaries, two complete turns,
restricted report continuations, cache headers, log markers and exit code zero
after `docker stop`. Containers use a read-only filesystem, dropped capabilities
and `no-new-privileges`. The temporary server publishes an ephemeral loopback port
and is removed after validation. Docker checks are separate from `scripts/verify.py`.

To serve manually, export a private random `BANKERIQ_DEMO_TOKEN`, then run:

```sh
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --publish 127.0.0.1:8088:8088 --env BANKERIQ_DEMO_TOKEN bankeriq-runtime:synthetic
```

The default command selects the mock model and binds `0.0.0.0:8088` inside the
container. The built-in health check targets port 8088; override it if changing
the server port. The token remains synthetic authentication and is visible to
local Docker administrators. Supply no customer data to this profile.

Validated on 2 November 2026 on Docker Desktop, Linux ARM64, Python 3.14.6:
the image build and all checks above passed. No Azure resources were provisioned.
Linux AMD64 and Foundry-hosted lifecycle validation remain pending. Conversations,
jobs and artifacts remain in memory and disappear on exit. PostgreSQL conversation
and job storage, Redis sessions, asynchronous workers and production identity are
unfinished integrations.
Real report storage and authenticated downloads belong to tool implementations.

## Live Foundry validation

Provision the [Terraform environment](../../../infra/azure/README.md), then export
its non-secret runtime settings:

```sh
terraform -chdir=infra/azure output -json runtime_environment > infra/azure/runtime.json
uv run bankeriq-runtime validate --provider foundry --config infra/azure/runtime.json
```

Add `--transcript scratch/transcripts/foundry.md` to retain a live conversation.
The provider and deployment selection appear in the transcript; Terraform records
the deployment's underlying model/version and region.

`FOUNDRY_AUTH_MODE=azure_cli` uses your `az login` session and the account-scoped
inference role. The token provider renews credentials in memory. API-key mode is
still available explicitly as `FOUNDRY_AUTH_MODE=api_key` with
`AZURE_OPENAI_API_KEY`; the Terraform environment disables API-key access.

Live validation makes billable inference calls. Each run is bounded to six model
steps, six tool calls and 256 output tokens per model request. It fails explicitly
if the model skips the required tool sequence, returns an unfinished answer or
cannot complete the continuation. It does not retry failed inference automatically.
Keep reasoning unset for the initial non-reasoning model compatibility check.

`--provider foundry` also works with `serve`. Model selection is explicit; failures
leave the selected provider unchanged. The initial runtime uses in-memory state
and loses conversations and pending jobs when the process exits.

Live validation passed on 2 November 2026 with regional `gpt-4o` in Sweden Central:
four model calls, three tool calls, one continuation and the answer 42. A separate
HTTP server process also completed two turns with real inference, rejected
unauthenticated/unapproved requests and shut down cleanly. The coordinator stayed
local and report delivery remained mocked. See the
[Azure validation record](../../../infra/azure/README.md#validation-evidence).
