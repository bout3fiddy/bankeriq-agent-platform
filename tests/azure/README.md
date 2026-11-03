# Azure runtime validation and operator conversations

This harness hosts the existing durable API/coordinator/tool composition with synthetic
arithmetic and restricted-report fixtures. It belongs to group 09 and is not a production
entry point. The [Terraform module](../../infra/azure/modules/runtime/README.md) owns all
cloud resources; the migration job owns application schema, prompt publication and SQL
roles. Only the coordinator receives inference permission; tools cannot call a model.

Admission verifies a delegated Entra v2 token, tenant, audience, issuer, lifetime, scope,
client and explicit user allowlist. Headers supply UUID conversation/run identifiers;
they cannot replace the signed identity. By default, the pilot admits only the exact text
`Calculate the sum of 2 and 3.` or
`Calculate the sum of 2 and 3, then prepare a restricted report.`. Permission/output checks
still apply. The authenticated report URL is a synthetic fixture with no Blob storage.

The explicitly enabled `runtime_interactive=true` profile accepts non-sensitive operator
conversations up to 8,192 UTF-8 bytes. It preserves Entra admission and the user allowlist;
it does not classify sensitive content. The [persistent deployment guide](../../infra/azure/LIVE.md)
records the current environment and chat commands. The fixed-input validator below applies
only to the default fixture policy; interactive input is validated with the operator CLI.

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
Coordinator maintenance also renews existing Redis views for live PostgreSQL leases,
including tools running without a connected reader. Accepted messages and explicit
POST /activity signals also renew retention. Passive history/status/SSE reads do not.
`runtime_session_idle_seconds` defaults to 1,800 seconds. Background lease scans run
every second in bounded pages. Once activity stops, expiry remains enabled. Reconstruction
uses PostgreSQL after cache loss, including when the next message arrives hours later.
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

## Live semantic-memory conversation

The queued application always constructs semantic memory and registers `retrieve_history`.
There is no conversation-level enable flag. Apply migrations 013–015 and runtime table grants
before releasing this image. Consult `LIVE.md` for the currently deployed image.

`semantic_chat` uses the normal application composition, checked-in prompt catalogue, real local
PostgreSQL/Redis and fresh coordinator/tool processes for each message. It captures fictional
inputs, responses, model-selected summaries, published summary versions and source cursors.
Its local database guard rejects non-loopback hosts. This is an operator validation harness.
It does not change cloud resources, deployed images, remote databases or prompt bindings.

Private Foundry is reachable through `exec_transport`, which keeps one authenticated exec
connection to the existing coordinator. Its ephemeral Python helper owns the managed-identity
SDK client and writes no files. A local owner-only Unix socket forwards bounded synthetic
requests; credentials stay in their respective processes. The helper exits on relay shutdown
or after 20 minutes. This transport is confined to validation code. Requests have no artificial
pacing; actual provider rejection status and bounded retry headers reach the normal worker.
Recorded turn/model timings include the relay and fresh local worker startup; use the
performance folder's normal API procedure for hosted latency measurements.

Start the dedicated local services from the group-11 instructions, then create a separate
local database. Set `memory_coordinator_id` to the existing coordinator's Azure resource ID;
this is an inspection/runtime-call target, never a resource creation request.

```sh
docker exec bankeriq-memory-pg createdb -U postgres semantic_live
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/semantic_live'
export BANKERIQ_TEST_REDIS_URL='redis://127.0.0.1:56399/1'
export BANKERIQ_TEST_RELAY_SOCKET='/tmp/bankeriq-semantic-relay.sock'
# Use the existing Foundry URL from terraform output -json runtime_environment.
export FOUNDRY_OPENAI_BASE_URL='<existing Foundry OpenAI base URL>'
uv run python -m tests.azure.exec_transport \
  --socket "$BANKERIQ_TEST_RELAY_SOCKET" --resource-id "$memory_coordinator_id"
```

Keep that relay terminal open. In another terminal with the same exported variables:

```sh
uv run python -m tests.azure.semantic_chat send \
  --state scratch/memory-conversation.json \
  --text 'Plan a fictional fair. Keep replies concise. No real purchases or bookings.'
# Repeat send with subsequent messages in the same conversation.
uv run python -m tests.azure.semantic_chat clear-cache \
  --state scratch/memory-conversation.json
uv run python -m tests.azure.semantic_chat send \
  --state scratch/memory-conversation.json --text 'Recall the decisions we made earlier.'
uv run python -m tests.azure.semantic_chat render \
  --state scratch/memory-conversation.json --output scratch/memory-transcript.md
```

`clear-cache` removes only this conversation's Redis view and records its durable cursor.
The next `send` reconstructs the view after completion. Every send already uses new worker
processes. Stop the relay with SIGTERM after validation; it sends `QUIT` to its temporary
helper and removes its local socket. Never stop or restart the deployed application for this test.
The recorded transcript includes failures instead of treating exhausted requests as successful.

## Replay and inspect hosted semantic memory

The normal queued pilot always wires semantic memory and `retrieve_history`; there is no
per-chat opt-in. The [fictional dialogue](semantic_dialogue.json) contains 18 turns that
exercise corrected decisions, long drafts, unresolved work, arithmetic and source retrieval.
Its arrays join with one space. Local `semantic_chat replay --dialogue` uses this same fixture.
The [local live transcript](../../build-order/evidence/11-local-live-model-transcript.md)
retains earlier failed attempts and every published summary, including cache-loss recovery.

For a separately authorised hosted release, publish the immutable image, update only the
existing migration job first, execute it successfully, then apply the full app image plan.
Migrations 013–015 are additive. Review both saved plans for unwanted replacements or deletion.
Do not set `runtime_ready=false` during this release: it would remove the app resources.
Use a targeted migration-job plan deliberately for this sequencing, then a full plan.

Copy `scratch/live-client.json` to a distinct file and use `chat --config <copy> new` so the
original conversation survives. Send each joined fixture message through `chat send` and
retain its JSON response, including status. The CLI waits up to 600 seconds by default;
`--wait-seconds` accepts 1 through 900. There is no process-local quota allowance. The model
adapter observes real provider rejections; workers persist a bounded retry deadline and release
their slot. Long turns may include multiple summary calls. Keep these correctness samples
separate from fixed short-answer latency cohorts.

For an explicitly requested synthetic transcript, export the exact owned PostgreSQL records
through the API app's authenticated exec channel:

```sh
az containerapp exec -g bankeriq-live-runtime -n bankeriq-li-c19afe58-api \
  --command 'python -m tests.azure.memory_probe --user <allowlisted-user-id> \
  --conversation <conversation-id> --run <run-id>'
```

Keep the command on one line inside the quoted argument when invoking Azure CLI.
The probe checks the allowlist and conversation owner, then exports bounded journal events,
summary versions/coverage, summary attempt records, tool joins and job counts. Its payload
belongs only in the requested reviewed transcript, never normal application telemetry.
`--clear-view` evicts only that conversation's Redis history key and checks reconstruction;
it preserves every PostgreSQL record and every other conversation's cache.

The [hosted transcript](../../build-order/evidence/11-hosted-live-model-transcript.md)
contains the deployed run's actual summary and its selection-quality limitations. Reproduce
its serial dialogue with fresh local filenames and the existing authorised operator login:

```sh
cp scratch/live-client.json scratch/semantic-client.json
uv run python -m tests.azure.chat --config scratch/semantic-client.json new
uv run python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

config = Path("scratch/semantic-client.json")
output = Path("scratch/semantic-hosted.json")
assert not output.exists(), "choose a fresh transcript path"
ledger = {"conversation": json.loads(config.read_text())["conversation"], "turns": []}
dialogue = json.loads(Path("tests/azure/semantic_dialogue.json").read_text())
for number, parts in enumerate(dialogue, 1):
    text = " ".join(parts)
    result = subprocess.run(
        [sys.executable, "-m", "tests.azure.chat", "--config", str(config), "send", "--text", text],
        capture_output=True, text=True, check=True,
    )
    turn = {**json.loads(result.stdout), "number": number, "user": text}
    ledger["turns"].append(turn)
    output.write_text(json.dumps(ledger, indent=2) + "\n")
    assert turn["status"] == "completed", "retain and inspect the failed turn"
PY
```

Save the probe's JSON output separately as `scratch/semantic-snapshot.json`, then render:

```sh
uv run python -m tests.azure.semantic_chat render --state scratch/semantic-hosted.json \
  --hosted-snapshot scratch/semantic-snapshot.json --output scratch/semantic-hosted.md
```

The renderer matches summary publication to its recorded run and rejects mismatched scopes.
Its generated transcript preserves exact published entries; keep analysis of omitted details
or inaccurate semantic labels beside the evidence instead of rewriting the saved summary.
