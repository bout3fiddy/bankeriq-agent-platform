-- All foreign keys retain tenant and conversation ownership on the writable primary.
CREATE TABLE runtime_generation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    recovery_id text NOT NULL
);
CREATE TABLE conversations (
    tenant_id text NOT NULL, conversation_id text NOT NULL, user_id text NOT NULL,
    revision bigint NOT NULL DEFAULT 0, admission_sequence bigint NOT NULL DEFAULT 0,
    cursor bigint NOT NULL DEFAULT 0, active_run_id text,
    deletion_generation bigint NOT NULL DEFAULT 0, deleted boolean NOT NULL DEFAULT false,
    last_activity timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, conversation_id)
);
CREATE TABLE runs (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    admission_sequence bigint NOT NULL, active boolean NOT NULL,
    revision bigint NOT NULL DEFAULT 0, status text NOT NULL,
    checkpoint jsonb NOT NULL CHECK (octet_length(checkpoint::text) <= 262144),
    format_version integer NOT NULL DEFAULT 1 CHECK (format_version = 1),
    PRIMARY KEY (tenant_id, conversation_id, run_id),
    UNIQUE (tenant_id, run_id), UNIQUE (tenant_id, conversation_id, admission_sequence),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations
);
CREATE UNIQUE INDEX one_active_run ON runs (tenant_id, conversation_id) WHERE active;
CREATE TABLE admissions (
    tenant_id text NOT NULL, request_id text NOT NULL, conversation_id text NOT NULL,
    run_id text NOT NULL, fingerprint text NOT NULL, admission_sequence bigint NOT NULL,
    PRIMARY KEY (tenant_id, request_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
CREATE TABLE conversation_events (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    sequence bigint NOT NULL, event_id text NOT NULL, payload jsonb NOT NULL,
    CHECK (octet_length(payload::text) <= 65536),
    PRIMARY KEY (tenant_id, conversation_id, sequence), UNIQUE (tenant_id, event_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
CREATE INDEX run_events ON conversation_events (tenant_id, conversation_id, run_id, sequence);
CREATE TABLE jobs (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    job_id text NOT NULL, operation text NOT NULL, operation_version integer NOT NULL DEFAULT 1,
    input_revision bigint NOT NULL, status text NOT NULL,
    due_at timestamptz NOT NULL DEFAULT now(), attempt integer NOT NULL DEFAULT 0,
    lease_owner text, lease_expiry timestamptz, ownership_token bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, conversation_id, job_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
CREATE INDEX due_jobs ON jobs (due_at, tenant_id, conversation_id) WHERE status = 'ready';
CREATE INDEX expired_jobs ON jobs (lease_expiry) WHERE status = 'leased';
CREATE TABLE results (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    call_id text NOT NULL, outcome jsonb NOT NULL, delivery jsonb,
    PRIMARY KEY (tenant_id, conversation_id, run_id, call_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs,
    CHECK (octet_length(outcome::text) <= 65536),
    CHECK (octet_length(delivery::text) <= 65536)
);
CREATE TABLE approvals (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    reference_id text NOT NULL, call_id text NOT NULL, argument_fingerprint text NOT NULL,
    decision boolean, expires_at timestamptz, replay_policy text NOT NULL DEFAULT 'single_use',
    PRIMARY KEY (tenant_id, conversation_id, run_id, reference_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
CREATE TABLE transitions (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    operation_id text NOT NULL, fingerprint text NOT NULL, response jsonb NOT NULL,
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs,
    CHECK (octet_length(response::text) <= 262144)
);
CREATE TABLE projection_work (
    tenant_id text NOT NULL, conversation_id text NOT NULL, dirty_cursor bigint NOT NULL,
    deletion_generation bigint NOT NULL, recovery_id text NOT NULL,
    projected_cursor bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, conversation_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations
);
CREATE INDEX dirty_projection ON projection_work (tenant_id, conversation_id)
    WHERE dirty_cursor > projected_cursor;
-- Storage bounds precede the later asynchronous materialisation implementation.
CREATE TABLE history_pages (
    tenant_id text NOT NULL, conversation_id text NOT NULL, first_cursor bigint NOT NULL,
    last_cursor bigint NOT NULL, format_version integer NOT NULL CHECK (format_version = 1),
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 1048576),
    PRIMARY KEY (tenant_id, conversation_id, first_cursor),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations,
    CHECK (last_cursor >= first_cursor AND last_cursor - first_cursor < 100)
);
