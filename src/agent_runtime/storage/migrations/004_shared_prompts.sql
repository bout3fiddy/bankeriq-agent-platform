CREATE TABLE prompt_bindings (
    application text NOT NULL, environment text NOT NULL,
    revision bigint NOT NULL DEFAULT 0, active_version text,
    PRIMARY KEY (application, environment)
);
CREATE TABLE prompt_versions (
    application text NOT NULL, environment text NOT NULL, version text NOT NULL,
    body text NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 32768),
    content_hash text NOT NULL, runtime_version text NOT NULL, runtime_hash text NOT NULL,
    composer_version text NOT NULL,
    author_tenant text NOT NULL, author_user text NOT NULL, validation_reference text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application, environment, version),
    FOREIGN KEY (application, environment) REFERENCES prompt_bindings
);
ALTER TABLE prompt_bindings ADD FOREIGN KEY (application, environment, active_version)
    REFERENCES prompt_versions (application, environment, version);
CREATE TABLE prompt_operations (
    application text NOT NULL, environment text NOT NULL, request_id text NOT NULL,
    fingerprint text NOT NULL, action text NOT NULL, version text NOT NULL,
    actor_tenant text NOT NULL, actor_user text NOT NULL, revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application, environment, request_id),
    FOREIGN KEY (application, environment, version) REFERENCES prompt_versions
);
CREATE UNIQUE INDEX prompt_activation_revision ON prompt_operations
    (application, environment, revision) WHERE action = 'activate';
CREATE TABLE prompt_withdrawals (
    application text NOT NULL, environment text NOT NULL, version text NOT NULL,
    request_id text NOT NULL,
    PRIMARY KEY (application, environment, version),
    FOREIGN KEY (application, environment, version) REFERENCES prompt_versions,
    FOREIGN KEY (application, environment, request_id) REFERENCES prompt_operations
);
-- Publication and audit records are immutable even when accessed outside the API.
CREATE FUNCTION reject_prompt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'published prompt records are immutable';
END;
$$;
CREATE TRIGGER immutable_prompt_version BEFORE UPDATE OR DELETE ON prompt_versions
    FOR EACH ROW EXECUTE FUNCTION reject_prompt_mutation();
CREATE TRIGGER immutable_prompt_operation BEFORE UPDATE OR DELETE ON prompt_operations
    FOR EACH ROW EXECUTE FUNCTION reject_prompt_mutation();
CREATE TRIGGER immutable_prompt_withdrawal BEFORE UPDATE OR DELETE ON prompt_withdrawals
    FOR EACH ROW EXECUTE FUNCTION reject_prompt_mutation();
CREATE TABLE model_step_prompts (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    step_number bigint NOT NULL, operation_id text NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id, run_id, step_number),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs,
    FOREIGN KEY (tenant_id, operation_id) REFERENCES transitions
);
