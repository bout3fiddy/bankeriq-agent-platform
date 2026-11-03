# Deferred runtime options

Status: planned. Preserved from the former group-12 backlog on 4 November 2026.
These options have no implementation slot or dependency on group 12's completion. Give a
selected option its own numbered group and measurable gates before starting implementation.

## Broker transport, conditional

If measured transport needs justify it, add transactional outbox/inbox adapters. Test
publish-before-ack crashes, duplicate and out-of-order delivery, schema compatibility and
replay retention. Preserve local atomic transitions and explicit reconciliation across
databases. Broker infrastructure and cross-database transactions remain outside the selected
PostgreSQL queue profile. See the [handoff design](../ARCHITECTURE.md#future-broker-handoff).

## Business prompt overlays

Add a trusted user-class or usage-profile binding over the global prompt with versioned
composition. Define assignment and change semantics before supporting multiple overlays;
global updates still propagate. Preserve pinned manifests for prepared work and keep prompt
selection separate from permission grants. See the existing
[prompt boundary](../ARCHITECTURE.md#shared-system-prompts-and-live-updates).

## Ownership of other former follow-ups

- Semantic compaction is implemented in
  [group 11](11-completed-semantic-compaction-and-retrieval.md).
- Streaming, measured scheduling and memory improvements belong to
  [group 12](12-planned-concurrent-sessions-streaming-and-memory-quality.md).
- Database growth and conditional tenant sharding belong to
  [group 14](14-planned-storage-lifecycle-and-capacity-controls.md).

No broker or prompt-overlay implementation is authorised or claimed by this backlog.
