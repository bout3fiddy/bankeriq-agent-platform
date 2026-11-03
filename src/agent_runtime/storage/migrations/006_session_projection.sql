-- Materialisation and cache acknowledgement advance independently. A cache loss
-- never advances projected_cursor or permits journal deletion.
ALTER TABLE projection_work ADD COLUMN cache_cursor bigint NOT NULL DEFAULT 0;
ALTER TABLE projection_work ADD COLUMN pending_events bigint NOT NULL DEFAULT 0;
ALTER TABLE projection_work ADD COLUMN pending_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE projection_work ADD COLUMN dirty_since timestamptz NOT NULL DEFAULT now();
ALTER TABLE projection_work ADD COLUMN materialized_at timestamptz;
UPDATE projection_work p SET
    pending_events = (SELECT count(*) FROM conversation_events e
        WHERE (e.tenant_id, e.conversation_id) = (p.tenant_id, p.conversation_id)
        AND e.sequence > p.projected_cursor),
    pending_bytes = COALESCE((SELECT sum(octet_length(e.payload::text))
        FROM conversation_events e
        WHERE (e.tenant_id, e.conversation_id) = (p.tenant_id, p.conversation_id)
        AND e.sequence > p.projected_cursor), 0);
CREATE INDEX projection_due ON projection_work (dirty_since, tenant_id, conversation_id)
    WHERE dirty_cursor > projected_cursor OR dirty_cursor > cache_cursor;
CREATE INDEX idle_conversations ON conversations (last_activity, tenant_id, conversation_id);
CREATE TABLE conversation_snapshots (
    tenant_id text NOT NULL, conversation_id text NOT NULL,
    cursor bigint NOT NULL, deletion_generation bigint NOT NULL, recovery_id text NOT NULL,
    format_version integer NOT NULL CHECK (format_version = 1),
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 1048576),
    PRIMARY KEY (tenant_id, conversation_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations
);

ALTER TABLE admissions ADD COLUMN accepted_cursor bigint NOT NULL DEFAULT 0;
UPDATE admissions a SET accepted_cursor = COALESCE((
    SELECT max(e.sequence) FROM conversation_events e
    WHERE (e.tenant_id, e.conversation_id, e.run_id) =
        (a.tenant_id, a.conversation_id, a.run_id)
    AND e.payload->>'type' = 'Message' AND e.payload->'value'->>'role' = 'user'
), 0);
