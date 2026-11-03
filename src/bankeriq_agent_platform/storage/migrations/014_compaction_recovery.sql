-- Keep applied format-1 summaries readable, with explicit new coverage metadata.
ALTER TABLE semantic_summaries DROP CONSTRAINT semantic_summaries_format_version_check;
ALTER TABLE semantic_summaries ADD CHECK (format_version IN (1,2));
ALTER TABLE semantic_summaries ADD COLUMN partial_turn boolean NOT NULL DEFAULT false;
ALTER TABLE semantic_summaries ADD COLUMN retirements jsonb NOT NULL DEFAULT '[]';
ALTER TABLE semantic_summaries ADD CHECK (octet_length(retirements::text) <= 65536);
ALTER TABLE semantic_operations ADD COLUMN partial_turn boolean NOT NULL DEFAULT false;

-- One clean overflow recovery per prepared step. Claiming the retry is durable:
-- a crashed caller cannot unknowingly issue another recovery request on restart.
CREATE TABLE model_context_recoveries (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    step_number integer NOT NULL, recovery_id text NOT NULL, deletion_generation bigint NOT NULL,
    policy_key text NOT NULL, request_fingerprint text NOT NULL,
    input_tokens integer NOT NULL CHECK (input_tokens >= 0),
    rejected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    retry_claimed boolean NOT NULL DEFAULT false,
    failure_reason text NOT NULL DEFAULT 'provider_context_overflow',
    PRIMARY KEY (tenant_id,conversation_id,run_id,step_number),
    FOREIGN KEY (tenant_id,conversation_id,run_id) REFERENCES runs,
    CHECK (failure_reason IN ('provider_context_overflow','retry_context_overflow'))
);
