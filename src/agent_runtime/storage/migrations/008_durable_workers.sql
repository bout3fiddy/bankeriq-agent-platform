-- Existing synchronous records remain inert until explicitly assigned recovery state.
ALTER TABLE jobs ADD COLUMN recovery jsonb;
ALTER TABLE jobs ADD COLUMN input_fingerprint text;
ALTER TABLE jobs ADD COLUMN settlement_fingerprint text;
ALTER TABLE jobs ADD COLUMN settlement_response jsonb;
ALTER TABLE jobs ADD COLUMN wait_expiry timestamptz;
ALTER TABLE jobs ADD CONSTRAINT bounded_recovery
    CHECK (octet_length(recovery::text) <= 60000);
ALTER TABLE runs ADD COLUMN worker_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN worker_budget integer;
CREATE UNIQUE INDEX worker_operation_identity ON jobs
    (tenant_id, (recovery->>'operation_id')) WHERE recovery IS NOT NULL;
CREATE INDEX worker_due ON jobs (operation, due_at)
    WHERE recovery IS NOT NULL AND status IN ('ready', 'retry', 'approval');
CREATE TABLE worker_limits (
    queue text PRIMARY KEY,
    concurrency integer NOT NULL CHECK (concurrency > 0),
    tenant_concurrency integer NOT NULL CHECK (tenant_concurrency > 0)
);

-- Notifications contain only an opaque ID. Delivery and listeners are optional:
-- the indexed ready/expiry scans are authoritative after restart or connection loss.
CREATE FUNCTION notify_runtime_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.recovery IS NOT NULL AND NEW.status IN ('ready', 'retry', 'approval') THEN
        PERFORM pg_notify('runtime_jobs', NEW.job_id);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER runtime_job_wakeup AFTER INSERT OR UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION notify_runtime_job();
