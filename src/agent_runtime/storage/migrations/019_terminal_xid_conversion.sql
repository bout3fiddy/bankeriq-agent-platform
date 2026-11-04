-- Preserve 016's applied bytes. Native xid8 -> xid conversion discards the epoch;
-- a text round-trip would fail once the full transaction ID exceeds 2^32 - 1.
CREATE OR REPLACE FUNCTION record_terminal_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('completed', 'failed', 'cancelled', 'limit_reached') AND
        OLD.status NOT IN ('completed', 'failed', 'cancelled', 'limit_reached') THEN
        NEW.terminal_written_at := clock_timestamp();
        NEW.terminal_xid := pg_current_xact_id()::xid;
    END IF;
    RETURN NEW;
END;
$$;
