# Follow-up runtime work

Status: planned after the infrastructure groups.

1. **Semantic context compaction** — Implemented in the
   [compaction and retrieval group](11-completed-semantic-compaction-and-retrieval.md).
   Its required queued profile retains the documented model-quality and capacity limits.
2. **Richer streaming and scheduling** — Extend the initial progress/reconnect path
   and tune fairness or priorities only when measured workloads require it.
3. **Database growth** — Follow the
   [storage lifecycle group](13-planned-storage-lifecycle-and-capacity-controls.md)
   for retention, quotas, archival, partitioning and capacity planning.
4. **Tenant sharding, conditional** — Implement trusted tenant routing only after
   measured write bottlenecks justify it. Co-locate all tenant conversation/queue/
   deduplication records. Test fenced shard moves and prompt resolution from the
   authoritative configuration writer; splitting one tenant needs a separate design.
5. **Broker transport, conditional** — If measured transport needs justify it, add
   transactional outbox/inbox adapters. Test publish-before-ack crashes, duplicate
   and out-of-order delivery, schema compatibility and replay retention. Preserve
   local atomic transitions and explicit reconciliation across databases. Broker
   infrastructure and cross-database transactions are outside the initial build.
6. **Business prompt overlays** — Add a trusted user-class or usage-profile binding
   over the global prompt with versioned composition. Define assignment and change
   semantics before supporting multiple overlays; global updates still propagate.
