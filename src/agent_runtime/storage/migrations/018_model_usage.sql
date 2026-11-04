-- Numeric observation ledger for scoped measurements and finite validation budgets.
-- No prompt, reply, provider error, request body or credential belongs in this table.
CREATE TABLE model_usage (
    observation_id text PRIMARY KEY,
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('conversation','summary')),
    elapsed_ms bigint NOT NULL CHECK (elapsed_ms >= 0),
    status_code integer NOT NULL,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id,conversation_id,run_id) REFERENCES runs
);
CREATE INDEX conversation_model_usage ON model_usage (tenant_id,conversation_id,run_id);
