-- A call ID identifies a single intent within a run, including approval replay.
CREATE UNIQUE INDEX unique_call_intent ON conversation_events
    (tenant_id, conversation_id, run_id, (payload->'value'->>'call_id'))
    WHERE payload->>'type' = 'ToolCall';
ALTER TABLE approvals ADD COLUMN consumed boolean NOT NULL DEFAULT false;
ALTER TABLE approvals ADD COLUMN operation_version text;
