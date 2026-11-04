# Maintaining performance evidence

Read [REQUIREMENTS.md](REQUIREMENTS.md), [the current index](README.md) and its latest
record before working here. Root AGENTS.md still applies. This folder owns targets,
measurement procedures and dated evidence. The current user-authorised latency goal includes
runtime measurements and fixes. The register
itself grants no authority for runtime changes, deployment changes or additional cloud services.

## Evidence rules

- Use uv for Python commands. Use existing measurement interfaces before adding probes.
- Preserve historical files. Add a UTC-dated record with a distinct suffix for another
  collection on the same date, then update the latest pointer in README.md.
- Never turn a single observation into a percentile claim or change a target merely
  to make current measurements pass. Separate warm/cold, serial/concurrent and tool/no-tool data.
- Commit only reviewed synthetic metadata: IDs, timestamps, statuses, durations, counts,
  safe error codes and environment settings. Exclude tokens, prompts, message bodies,
  raw exceptions, SQL parameters, tool results and protected report references.
  Explicitly reviewed non-sensitive benchmark fixture text may be documented for reproduction;
  it is separate from captured application transcripts and telemetry.
- Raw operator output stays in ignored scratch/. Export a small allowlisted JSON record
  and a Markdown interpretation here. Do not copy whole CLI or Azure log responses blindly.
- Record missing measurements as null/unmeasured. Do not infer token counts from word
  counts or claim pure model time from an entire inference-worker span.
- Keep diagnostics passive when observing an idle conversation. Sending a message or
  POST /activity renews activity. Preserve scratch/live-client.json and its conversation.
- Apply existing task authorisation to live calls and load. A request to update documentation
  does not require new inference traffic. Do not provision resources to refresh a record.

## Refresh existing evidence without sending a message

Run from the repository root. Check git status, the deployment guide and current Terraform
outputs first. [infra/azure/LIVE.md](../infra/azure/LIVE.md) describes the operator environment;
its resources may later be removed or replaced. If the environment is unavailable, record
that limitation. Authentication uses the existing authorised Azure CLI login.

Create a distinct ignored collection directory and copy the non-secret client configuration:

```sh
performance_run_dir="scratch/performance/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p scratch/performance
mkdir "$performance_run_dir"
cp scratch/live-client.json "$performance_run_dir/client.json"
chmod 600 "$performance_run_dir/client.json"
git rev-parse HEAD > "$performance_run_dir/source-commit.txt"
terraform -chdir=infra/azure output -json hosted_foundation \
  > "$performance_run_dir/foundation.json"
terraform -chdir=infra/azure output -raw resource_group_name \
  > "$performance_run_dir/resource-group.txt"
```

Treat a directory-name collision as an error; choose a new collection directory. Do not
overwrite an earlier collection. Inspect endpoint/audience against current outputs before
using a copied client file. Client configuration contains opaque IDs and endpoints, never tokens.

Capture current deployment settings and passive inspection:

```sh
performance_group=$(cat "$performance_run_dir/resource-group.txt")
az containerapp list -g "$performance_group" --query \
  '[].{name:name,ready:properties.latestReadyRevisionName,
  minReplicas:properties.template.minReplicas,maxReplicas:properties.template.maxReplicas,
  containers:properties.template.containers[].{image:image,cpu:resources.cpu,
  memory:resources.memory}}' \
  -o json > "$performance_run_dir/apps.json"
uv run python -m tests.azure.chat --config "$performance_run_dir/client.json" inspect \
  > "$performance_run_dir/inspection.json"
performance_workspace=$(uv run python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["log_workspace"])' \
  "$performance_run_dir/foundation.json")
az monitor log-analytics query --workspace "$performance_workspace" --analytics-query \
  "$(cat performance/queries/recent-runs.kql)" -o json \
  > "$performance_run_dir/lifecycle.json"
```

The query exports metadata only for the last 24 hours; it cannot recover expired logs.
Filter by run_id from the client/measurement record when analysing a particular turn.
Log Analytics ingestion can lag by several minutes. Missing recent rows are not proof
that no work occurred. For immediate inspection, use an app name observed in apps.json:

```sh
az containerapp logs show -g "$performance_group" -n '<observed-app-name>' \
  --tail 100 --format text > "$performance_run_dir/console.log"
```

Keep that console export local until its contents are reviewed. These commands query Azure
and session metadata; they do not create a conversation, renew activity or submit a model call.
The group-12 revision-8 image emits correlated job/attempt spans, eligible streamed text and
passive process/queue samples. Application Insights tracing is not implemented. Verify the
deployed digest before interpreting those fields. Client
timings require capture rather than reconstruction from ingestion timestamps.

## Collect a fresh authorised sample

Use the copied configuration to create a separate conversation. This leaves the user's
idle experiment and default client configuration intact. The following is a short-response
smoke sample, not a representative percentile benchmark:

```sh
uv run python -m tests.azure.chat --config "$performance_run_dir/client.json" new \
  > "$performance_run_dir/conversation.json"
uv run python -m tests.azure.chat --config "$performance_run_dir/client.json" send \
  --text 'Explain what a bank statement is in one short sentence.' \
  > "$performance_run_dir/turn-001.json"
```

For a comparable cohort, declare the fixture, history size, warmup, count and concurrency
before collection. Two concurrent conversations need separate client files; never run
concurrent sends against one mutable client file. Keep setup/authentication timing separate
from timed requests, and document that the current CLI excludes token acquisition.
Fetch lifecycle data after ingestion catches up, retaining safe fields only.

The CLI supplies admission_seconds, turn_seconds, status and correlation IDs. It waits
up to 600 seconds by default (`--wait-seconds` accepts 1 through 900) and can return a
nonterminal status; check status explicitly. Review
the response and expected tool behavior locally before removing payloads from the retained
record. Preserve failed/timeout counts. Current `model.request` events report actual token usage,
numeric provider duration, HTTP status
and effective quota. Older releases lack those fields. The streaming reference client records
first visible text in `first_text_seconds`; the older polling CLI cannot measure it. Separate
summary calls from coordinator calls, and identify the client and image used in each record.

## Attribute latency correctly

- Use the emitted event timestamp, not Log Analytics ingestion time. KQL accesses the
  time property as e["time"]. Its ingestion delay is unrelated to conversation latency.
- model.request elapsed_ms covers the provider adapter call and validated response, using a
  monotonic clock. Match run_id and summary; do not replace actual usage with tokenizer estimates.
- worker.settled elapsed_ms covers the entire claimed attempt, including policy checks,
  preparation, handler execution and settlement. The model_infer worker includes database
  reads and commits as well as the provider call. It is not pure generation time.
- Calculate queue/claim gaps between admission.accepted and the preparation claim, then
  between preparation settlement and the inference claim. These also include claim work.
- The gap from worker settlement to cache.miss approximates completion detection and the
  beginning of final history retrieval. It excludes the rest of that response delivery.
- Client admission time overlaps these server intervals. Never add it to a complete
  server timeline. Preserve clock-alignment assumptions for cross-process timestamps;
  prefer monotonic durations within a process.
- Inspection's optional database_clock_timing reports monotonic work before and after
  the primary timestamp query. The client subtracts those known durations from its
  clock-offset uncertainty, retaining network/query uncertainty. Older images retain
  the whole round-trip interval. Invalid server durations fail measurement setup.
  Keep the observed offset interval and server durations with each attempt; do not
  substitute midpoint estimates for conservative delivery bounds.
- Failed attempts, retries, parallel tool spans and restarts need explicit matching by
  run, job/attempt and stage. Historical logs omit job IDs; newer group-12 source emits them.
  Do not invent attribution when simultaneous same-queue attempts are ambiguous.

The [initial record](records/2026-11-03-azure-pilot.json) retains one unambiguous serial
timeline and its limits. Other existing local measurements are available through
`uv run python -m scripts.measure_sessions`; follow the disposable-service setup in the
[session guide](../src/agent_runtime/sessions/README.md). That script writes data
and runs migrations. Never point its test DSNs at the retained Azure deployment. Keep
local session microbenchmarks separate from hosted conversation measurements.

## Update the register

1. Add records/YYYY-MM-DD-description.json with collection timestamps, source commit,
   deployed image/revision, model/version, host/region, replicas, queue/poll settings,
   workload identity, cache state, warmup, attempts, failures and safe raw measurements.
   Record unknown settings explicitly. Avoid exporting credentials or private config.
2. Add a matching Markdown report with executed commands, correctness results, sample
   count, timing boundaries, statistics method, applicable target IDs and remaining gaps.
   Calculate p50/p95 only for comparable cohorts using REQUIREMENTS.md. State eligibility
   and sample limitations beside every threshold comparison.
3. Update README.md with the latest dated record and qualification status. Keep links to
   earlier records and distinct profiles. Do not silently rewrite historical measurements.
4. Review JSON fields and Markdown links; run the documentation checks below. If runtime
   or harness code changed, also run the root verification gate with real local services.
   Record actual results; skipped service tests do not validate service behavior.

```sh
uv run python -m scripts.linting.check_json
uv run python -m scripts.linting.check_line_length
git diff --check
```

This procedure does not authorise a commit, push, deployment or a new monitoring service.
Follow the current user's task scope and preserve an already authorised active deployment.

## Fixed short-answer and summary workloads

The retained workloads under `workloads/` use synthetic data and the existing authenticated
runtime. `short_answers.py` creates a new conversation per attempt, preserves all attempted
outcomes, separates one warmup and checks the exact response `READY`. Declare the per-worker
arrival interval: the recorded pilot uses 3 seconds for one worker and 6 seconds for two,
up to 20 admitted messages/minute in each cohort. The two-worker cohort submits overlapping
pairs. This characterizes that offered load, not saturation throughput. Idle spacing is outside
turn timing and is solely a benchmark input; the runtime has no corresponding quota delay.

```sh
uv run python performance/workloads/short_answers.py \
  --config scratch/live-client.json --concurrency 1 --interval-seconds 3 --count 100 \
  --output scratch/short-serial-new.json
uv run python performance/workloads/short_answers.py \
  --config scratch/live-client.json --concurrency 2 --interval-seconds 6 --count 100 \
  --output scratch/short-paired-new.json
```

Run these cohorts sequentially with other inference experiments stopped. Config is read-only;
new conversations leave the original chat untouched. After log ingestion, export numeric
observations with `queries/model-requests.kql`, match by run_id and verify actual output tokens,
status, model-call count and quota. A missing observation remains an evidence gap until recovered.
For this one-call workload, subtract the observed adapter duration from client turn duration to
estimate overhead. This includes network/queue/storage/delivery and is not a server-only span.

`summary_generations.py` uses four reviewed fictional source fixtures, five repetitions each,
through the private relay documented in `tests/azure/README.md`. It validates excerpts using
normal runtime source selection and preserves failed attempts and unresolved retirement advice.
It does not read or write a database. Use the already-authorised model and owner-only socket:

```sh
PYTHONPATH=. uv run python performance/workloads/summary_generations.py \
  --socket /tmp/bankeriq-semantic-relay.sock --output scratch/summary-samples-new.json
```

This probe's output includes reviewed synthetic excerpts for local quality review. Export only
allowlisted numeric metadata, case identity and correctness counts into performance records;
application transcripts belong in the explicitly requested build-order evidence. Keep the
20-sample summary speed/correctness gate separate from qualified latency percentiles.

## Memory quality suite

`workloads/memory_quality_cases.json` defines twenty English, Dutch and mixed-language
scenarios. The runner repeats each twice, using isolated local PostgreSQL history and the
live model through the private relay. It requires local state, explicit campaign allocations
and a fresh output path. The two long cases require five actual compactions; the tool case
executes the synthetic calculation through the normal durable queue.

Use a fresh relay per case (`--case <fixture-id>`) for long evaluations. The operator exec
channel reset twice during longer suites; each unknown call retained its full reservation.
The relay now rotates after two minutes between completed requests and logs channel-open times.
New records retain remote SDK duration and operator-channel setup separately from the full
adapter round trip. Earlier absent timing remains unmeasured; never subtract an estimated
reconnect delay. Read the [timing boundaries](../tests/quality/README.md#timing-boundaries).
Its twenty-minute default helper deadline remains bounded; the explicit one-hour option can
cover a full suite but does not guarantee connection survival. Supply the observed coordinator
resource ID using the Azure harness guide.
The runner defaults to ten seconds between provider dispatches; pacing is outside generation
timing and does not exist in the application runtime. Keep other model experiments stopped.
The v10 campaign declares seven seconds between starts. The 139-call prior batch averaged
4,948 reserved input/output tokens per call, placing that fixture mix near 42,411 tokens/min
before connection setup. This estimate is a workload choice, not quota enforcement or a latency
claim; real rejection/unknown usage still stops the evaluator. Retain its actual dispatch setting.
The supplemental `workloads/memory_retrieval_cases.json` supplies capacity-driven omission cases;
select it using `--cases-file`. Read the [fixture rationale](../tests/quality/README.md) and retain
the selected question and fixture digest. A correctly remembered detail cannot qualify retrieval.

```sh
uv run python -m performance.workloads.memory_quality \
  --socket /tmp/agent-group12-quality.sock --output scratch/memory-quality-new.json \
  --budget-calls 250 --budget-input-tokens 750000 --budget-output-tokens 30000 \
  --dispatch-interval 10
```

Those allocations are an example, not new spending authority. Reconcile them against remaining
campaign usage before execution. Unknown usage keeps its reservation and stops later calls.
Preserve failed cases. The exit gate requires both repetitions, every critical fact, required
retrieval after actual omission, and at least 95% of other facts. Separately review the retained
synthetic source selections and summaries for relevance, stale facts and action-state fidelity;
exact answer matching cannot replace that review. Export only numeric allowlisted evidence.

## Mixed ordinary and compaction workload

The [mixed runner](../tests/mixed/README.md) prepares a finite paired-arrival experiment at
ten turns/minute. It separately records ordinary turns and memory turns with zero/one/multiple
actual publications. Its full per-run attempt reservations require a separately allocated
budget. The [hosted record](records/2026-11-04-hosted-mixed.md) includes 101 one-compaction turns,
actual provider overlap, all scheduled arrivals and matched publication timings. Local
accounting tests and hosted measurements remain distinct evidence.

## Finite concurrency campaign accounting

The current hosted workload accepts `--worker-budget 8`, selecting the existing durable run
attempt budget. Normal application admissions still default to 100. Every summary and context
recovery call consumes that same allowance. Reserve the expected ordinary cohort plus every
in-flight run's entire remaining allowance before starting; unknown outcomes keep the full
reservation. The campaign ledger is local and includes earlier unknown-usage reservations.

For 100 measured turns, one warmup, five concurrent sessions and 75 output tokens per expected
answer, the conservative allocation is 141 calls, 509,480 input and 48,535 output tokens:

```sh
uv run python -m performance.workloads.concurrent_answers \
  --config scratch/authorised-client.json --output scratch/fresh-five-session.json \
  --stop scratch/STOP-fresh-five-session --sessions 5 --count 100 --worker-budget 8 \
  --tenant '<authorised-tenant-id>' --user '<authorised-operator-id>' \
  --image-digest '<verified-current-digest>' --max-output-tokens 75 \
  --budget-input-tokens 509480 --budget-output-tokens 48535
```

These bounds need allocation from an already authorised campaign; this example grants none.
Run one inference campaign at a time and charge its reconciled usage before starting another.
The harness holds a client slot through accepted subscription-rejection recovery. It retries
only read-only subscriptions for the original run, preserves the rejection, and excludes recovery
from successful first-pass latency samples. Uncertain admission acknowledgement or unresolved
usage stops future arrivals. Do not release reservations merely because the client disconnected.
The [regression tests](../tests/unit/azure/test_workload_recovery.py) cover both boundaries.
