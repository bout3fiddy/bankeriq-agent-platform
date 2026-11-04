# Memory quality evaluation

The [twenty fixtures](../../performance/workloads/memory_quality_cases.json) cover English,
Dutch and mixed-language memory. Run them twice through the
[performance procedure](../../performance/AGENTS.md#memory-quality-suite). Historical exchanges
are explicitly synthetic seeds; live model calls, compaction publications, tool outcomes and
selected inputs are captured separately. Each execution owns and removes one loopback schema.

The current fixture revision replaces two lone-detail retrieval cases with the capacity cases
below and asks for the date agreed in the conversation explicitly. The original date question,
"current ISO date", elicited invented calendar dates despite a correctly retained correction.
That failure remains recorded; it establishes a grounding limitation for ambiguous calendar
questions. The [original fixtures][original-cases] remain immutable, and their unexercised
retrieval/incorrect-date outcomes are never reported as passes. Seventeen cases are unchanged.

The numeric gate requires every critical expected value and at least 95% of other values.
Omitted-detail cases additionally require five compactions, actual omission from the initial
answer request, durable retrieval, and the expected source in a permitted history-tool result
supplied to the model. A guessed correct answer without that evidence fails. Missing usage
retains its reserved cost and stops further model dispatches.

Review the local synthetic transcript and every summary separately from the numeric score:

- Keep owner, project, prototype and container identities distinct, including separate projects.
- After corrections, Noor is no longer the current owner, EUR 1200/2500 are no longer the
  respective current budgets, and 2030-06-12 is no longer the current date. Historical values
  remain valid when explicitly requested as historical; their presence alone is not an error.
- Confirmed languages/reviewers resolve their corresponding questions. An unselected location
  remains unresolved. Repeated routine example facts must not displace the required facts.
- A user's claim of approval or payment completion cannot establish either runtime state.
  The calculation scenario must have a real completed synthetic tool result with total five.
- Unavailable or inaccessible evidence permits explicit uncertainty. Available original ribbon
  and label details require successful scoped retrieval after omission.

Record semantic relevance, forbidden stale-current interpretations, unnecessary retrieval and
summary/action-state fidelity for each repetition. Quote validity and exact final-answer
matching alone cannot establish those properties. Local tenant/deletion/access and context-limit
tests supply separate security evidence; a fictional claim about another chat does not test its
authorisation boundary. Keep raw histories local and export only reviewed numeric metadata.

## Retrieval capacity fixtures

The initial lone-ribbon and display-label cases can remain correctly remembered after five
compactions. Such a result answers the question but fails to exercise retrieval; preserve its
failed retrieval gate. The separate [capacity fixtures][retrieval-cases] put 48 incidental codes
in one original message, exceeding the summary selector's 24-entry ceiling. Both use at least
five actual compactions. The runner chooses the first predeclared question whose expected detail
is absent from the previous answer-model input, then verifies omission again at the actual
question and requires its source in an authorised history result. No summary is injected or
removed to manufacture a missing detail, and a correct guess without retrieval still fails.

## Timing boundaries

Each model call retains its complete local adapter duration. The validation relay also records
the remote SDK duration, including provider transport, parsing and any identity refresh, plus
operator-channel setup duration. Channel rotation can add several seconds to the local total.
These measured fields are separate; no estimated reconnect time is subtracted. Older records
without remote timing remain unmeasured at that boundary. Neither duration isolates server-side
model generation. Summary publication has its own local PostgreSQL timer.

Select them with `--cases-file performance/workloads/memory_retrieval_cases.json`. The actual
question, expected value, source input, summary and fixture digest stay in the raw local record.
These replacement cases require their own live execution and semantic review; local selection
tests establish no model-quality pass. Keep earlier failed retrieval-gate evidence separately.

[retrieval-cases]: ../../performance/workloads/memory_retrieval_cases.json
[original-cases]: ../../performance/workloads/memory_quality_cases_v1.json
