-- Existing rows retain unknown timing; migration time is not their admission time.
ALTER TABLE runs ADD COLUMN admitted_at timestamptz;
ALTER TABLE runs ALTER COLUMN admitted_at SET DEFAULT clock_timestamp();
ALTER TABLE runs ADD COLUMN terminal_written_at timestamptz;
ALTER TABLE runs ADD COLUMN terminal_xid xid;

-- The write timestamp is a lower bound for commit, suitable for a conservative
-- delivery-delay upper bound. Exact commit time additionally requires PostgreSQL
-- track_commit_timestamp; readers report absence when that facility is disabled.
CREATE FUNCTION record_terminal_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('completed', 'failed', 'cancelled', 'limit_reached') AND
        OLD.status NOT IN ('completed', 'failed', 'cancelled', 'limit_reached') THEN
        NEW.terminal_written_at := clock_timestamp();
        NEW.terminal_xid := pg_current_xact_id()::text::xid;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER terminal_run_timing BEFORE UPDATE ON runs
    FOR EACH ROW EXECUTE FUNCTION record_terminal_run();
