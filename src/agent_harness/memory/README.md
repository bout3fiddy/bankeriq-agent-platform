# Harness memory policy

Pure decisions over bounded conversation data. Runtime storage supplies a `MemoryWindow`;
the harness selects permitted context and compaction inputs without database or network I/O.

- `semantic.py`: immutable facts, token budgets, deterministic candidates and source validation.
- `retirement.py`: bounded selection/protection and validated replacement/resolution advice.
- `projection.py`: current visibility policy and safe journal-to-context projection.
- `assembly.py`: summary instructions/schema, request assembly and compaction-prefix selection.
- `window.py`: source-window data shared with runtime persistence.
- `retrieval.py`: history-tool definition and bounded argument validation.

For example, `plan_compaction()` can select completed turns 1 and 2 while retaining turns
3 and 4 verbatim. The [runtime service](../../agent_runtime/memory/README.md) must reserve
that exact target, call the model, recheck current authority and publish under its worker fence.
A selection is data; it does not itself execute a model request or authorise a write.
