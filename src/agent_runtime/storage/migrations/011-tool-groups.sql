-- A group owns ordered child identities and exactly one saved continuation.
CREATE TABLE tool_groups (
    tenant_id text NOT NULL, conversation_id text NOT NULL, run_id text NOT NULL,
    group_id text NOT NULL, failure_policy text NOT NULL
        CHECK (failure_policy IN ('wait_all','fail_fast')),
    continuation jsonb NOT NULL CHECK (octet_length(continuation::text) <= 60000),
    joined boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, conversation_id, run_id, group_id),
    FOREIGN KEY (tenant_id, conversation_id, run_id) REFERENCES runs
);
ALTER TABLE jobs ADD COLUMN group_id text;
ALTER TABLE jobs ADD COLUMN call_id text;
ALTER TABLE jobs ADD COLUMN call_position integer;
ALTER TABLE jobs ADD COLUMN resource_key text;
ALTER TABLE jobs ADD COLUMN child_outcome jsonb;
ALTER TABLE jobs ADD COLUMN child_delivery jsonb;
ALTER TABLE jobs ADD CONSTRAINT child_owner
    FOREIGN KEY (tenant_id, conversation_id, run_id, group_id) REFERENCES tool_groups;
ALTER TABLE jobs ADD CONSTRAINT child_shape CHECK (
    (group_id IS NULL AND call_id IS NULL AND call_position IS NULL) OR
    (group_id IS NOT NULL AND call_id IS NOT NULL AND call_position >= 0)
);
ALTER TABLE jobs ADD CONSTRAINT bounded_child_outcome
    CHECK (octet_length(child_outcome::text) <= 60000);
CREATE UNIQUE INDEX child_call ON jobs (tenant_id, conversation_id, run_id, call_id)
    WHERE group_id IS NOT NULL;
CREATE UNIQUE INDEX child_position ON jobs
    (tenant_id, conversation_id, run_id, group_id, call_position) WHERE group_id IS NOT NULL;
CREATE INDEX resource_queue ON jobs (tenant_id, resource_key, status)
    WHERE resource_key IS NOT NULL;
