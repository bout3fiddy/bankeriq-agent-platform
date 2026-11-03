# Follow-up runtime work

Status: planned after the infrastructure groups.

1. **Semantic context compaction** — Implement the
   [compaction and retrieval group](11-planned-semantic-compaction-and-retrieval.md),
   including durable action/decision summaries and bounded model-accessible history.
2. **Richer streaming and scheduling** — Extend the initial progress/reconnect path
   and tune fairness or priorities only when measured workloads require it.
3. **Database growth** — Partition/archive large event and job tables; consider
   larger primary capacity before adding write shards. Add read replicas only for
   measured lag-tolerant demand; they do not increase write capacity.
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
