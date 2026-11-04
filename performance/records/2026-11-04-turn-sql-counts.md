# Synthetic turn SQL count, 4 November 2026

Two correct fresh synthetic turns each submitted 179 SQL statements across admission,
preparation and inference. [Counts by caller](2026-11-04-turn-sql-counts.json) preserve pipeline
status and source locations. This structural diagnostic identifies remaining database work;
it provides no latency percentile or evidence for removing ownership/permission checks.

| Stage | Statements per turn |
| --- | ---: |
| Durable admission | 21 |
| Preparation claim | 18 |
| Preparation execution/settlement | 48 |
| Inference claim | 18 |
| Inference execution/settlement | 74 |

The probe used real local PostgreSQL 17.11, a four-connection pool, the current worker/storage
implementation and an immediate synthetic READY model. It created and removed its own schema.
Creation, migration and final correctness reads were excluded from these counts. It instruments
Connection.execute and records caller locations only; no SQL parameters or payloads are exported.
Pipelined statements are counted individually, so 179 statements do not imply 179 network trips.
Implicit transaction commands and hosted prompt-resolution checks are outside this count.
The fixed prompt resolver, absent HTTP/SSE/Redis and explicit sequential worker execution also
differ from the hosted workload. Stack tracing changes timings, which are deliberately omitted.

```sh
export BANKERIQ_TEST_POSTGRES_DSN='postgresql://postgres:synthetic@127.0.0.1:55449/postgres'
PYTHONPATH=. uv run python scratch/group12/count-turn-sql.py
```

The source was HEAD `54440b894deefad6dbfefe70252bfab37013fa84` plus group-12 changes. Further
optimisation requires a controlled candidate and correctness evidence; this count alone does
not justify removing a transaction, permission check, generation fence or unknown-effect record.
