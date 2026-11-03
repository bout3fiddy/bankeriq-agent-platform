# Build-order guidance

## Purpose and contents

This folder is the canonical implementation roadmap. Start with [README.md](README.md)
for sequence, current next work, status definitions and scope. Numbered Markdown files
contain one task group each: requirements, dependencies, completion gates, evidence
and remaining limits. [design-references.md](design-references.md) records design
sources and review provenance. Runtime details remain in the linked component guides.

These instructions supplement the repository's [AGENTS.md](../AGENTS.md). Read the
root README, ARCHITECTURE, components and STYLEGUIDE before implementation, plus
applicable nested instructions in the source directories you change.

## Selecting and executing work

- Open the requested group's file and read its entire scope, dependencies and gates.
  For a request to do the next group, consult the index and verify prerequisites.
  Completed groups supply evidence and constraints; their presence is no new task.
- Follow declared dependencies. File numbers preserve roadmap order without making
  unrelated follow-up work an implicit prerequisite. Keep later features deferred
  unless the user changes scope.
- Distinguish proposed requirements, implemented behaviour and executed validation.
  Consult current code and component guides when evidence may be stale. Record a
  discrepancy explicitly; do not silently remove an unmet requirement.
- Implement the group's code, meaningful tests and supporting documentation together.
  Keep database/network clients and scheduling outside core runtime logic. Use
  synthetic data for routine checks and follow the group's real-service test gates.
- A plan item never authorises cloud provisioning, deployment or publication. Apply
  the root action rules and the user's current authorisation.

## Status and evidence maintenance

- Use exactly three statuses in filenames, Status paragraphs and index entries:
  `planned`, `in-progress` and `completed`.
- `planned`: work has not started.
- `in-progress`: work has started and is not yet completed. Set this status as soon
  as work starts on a planned group, and keep it until all required work is finished.
- `completed`: all requirements, tests and documentation for the group are finished
  and its acceptance gates have passed. Record validation evidence inside the file.
- When status changes, rename the file and update every inbound Markdown link,
  including this folder's index and any links elsewhere in the repository. Update
  the index's next-group statement when a milestone changes. Preserve stable numbers
  unless the user explicitly changes ordering; describe dependency changes in text.
- Keep each requirement and its evidence in the owning group. Link from the index
  and related groups instead of duplicating task lists. Preserve dated historical
  evidence and distinguish it from newer results; retain hashes and source URLs.
- Record executed commands, service versions, outcomes and gaps. Model/tool permission
  checks and output isolation require behavioural evidence; design references alone
  do not validate implementation. Keep secrets and private data out of all examples.
- Keep generated transcripts and raw artifacts in the gitignored `evidence/` folder.
  Preserve them locally; do not force-add them. Keep reproducible commands, results and
  limitations in tracked group files. Evidence links require the local validation artifacts.
- Add new requirements to the relevant group or a separate explicitly scoped group,
  with dependencies and acceptance gates. Keep conditional work labelled conditional.

## Editing and verification

Keep authored lines at 100 characters or fewer under STYLEGUIDE rules. Resolve links
relative to each Markdown file; root source paths usually need `../` from this folder.
Check file targets, heading anchors and reference-style definitions after moves or
renames. Search the repository for the old path so guidance remains discoverable.

Run `uv run python scripts/verify.py` for relevant changes and `git diff --check`.
For documentation-only restructuring, also verify links and that requirements,
completion criteria and historical evidence survived the move. Report service tests
that skipped; do not present them as executed. Follow root instructions for commits.
