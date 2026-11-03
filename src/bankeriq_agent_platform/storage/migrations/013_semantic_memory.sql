-- Summaries are derived, versioned conversation data. Journal and execution records survive.
CREATE TABLE semantic_summaries (
    tenant_id text NOT NULL, conversation_id text NOT NULL, version bigint NOT NULL,
    operation_id text NOT NULL, recovery_id text NOT NULL, deletion_generation bigint NOT NULL,
    policy_key text NOT NULL, covered_turn bigint NOT NULL, covered_cursor bigint NOT NULL,
    format_version integer NOT NULL CHECK (format_version = 1),
    prompt_manifest jsonb NOT NULL, facts jsonb NOT NULL,
    CHECK (octet_length(facts::text) <= 65536),
    PRIMARY KEY (tenant_id, conversation_id, version),
    UNIQUE (tenant_id, conversation_id, operation_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations
);
CREATE TABLE semantic_operations (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    operation_id text NOT NULL, expected_version bigint NOT NULL, target_turn bigint NOT NULL,
    target_cursor bigint NOT NULL, recovery_id text NOT NULL, deletion_generation bigint NOT NULL,
    policy_key text NOT NULL, prompt_manifest jsonb NOT NULL, input_fingerprint text NOT NULL,
    attempts integer NOT NULL DEFAULT 0, last_attempt_id text, result_version bigint,
    PRIMARY KEY (tenant_id, conversation_id, operation_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
-- A retrieval retry reuses its charged, bounded result. The current policy and
-- generation are still checked before reuse, so revocation cannot revive cached access.
CREATE TABLE semantic_retrievals (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    operation_id text NOT NULL, fingerprint text NOT NULL, policy_key text NOT NULL,
    recovery_id text NOT NULL, deletion_generation bigint NOT NULL,
    result jsonb NOT NULL CHECK (octet_length(result::text) <= 32768),
    PRIMARY KEY (tenant_id, conversation_id, run_id, operation_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
