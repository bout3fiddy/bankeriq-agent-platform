-- A short, primary-only admission gate supports bounded backlog and release drain.
CREATE TABLE runtime_admission (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    open boolean NOT NULL DEFAULT true,
    bounded boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 0,
    global_limit integer NOT NULL DEFAULT 128 CHECK (global_limit BETWEEN 1 AND 10000),
    tenant_limit integer NOT NULL DEFAULT 32 CHECK (tenant_limit BETWEEN 1 AND global_limit),
    conversation_limit integer NOT NULL DEFAULT 2
        CHECK (conversation_limit BETWEEN 1 AND tenant_limit)
);
INSERT INTO runtime_admission (singleton) VALUES (true);
-- Application startup explicitly selects bounded admission. Existing generic
-- worker fixtures and synchronous profiles retain their previous capacity policy.
CREATE TABLE runtime_release_changes (
    request_id text PRIMARY KEY,
    actor_tenant text NOT NULL,
    actor_user text NOT NULL,
    requested_open boolean NOT NULL,
    expected_revision bigint NOT NULL,
    result_revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX pending_admission_runs ON runs (tenant_id, conversation_id)
    WHERE status IN ('queued', 'running', 'waiting');
