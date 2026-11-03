-- Keep execution history while adding safe inspection and optimistic recovery controls.
ALTER TABLE jobs ADD COLUMN failure_reason text NOT NULL DEFAULT 'none' CHECK (failure_reason IN (
    'none', 'process_lost', 'crash_limit', 'attempts_exhausted', 'run_budget_exhausted',
    'unknown_effect', 'execution_retry', 'approval_expired', 'access_denied',
    'approval_denied', 'cancelled', 'handler_unavailable', 'lookup_unavailable'
));
ALTER TABLE jobs ADD COLUMN crash_count integer NOT NULL DEFAULT 0 CHECK (crash_count >= 0);
ALTER TABLE jobs ADD COLUMN control_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN effect_pending boolean NOT NULL DEFAULT false;
ALTER TABLE jobs ADD COLUMN prepared_token bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN released_token bigint NOT NULL DEFAULT 0;
UPDATE jobs SET effect_pending=true WHERE recovery->>'phase' IN ('dispatch','reconcile','retry')
    AND settlement_fingerprint IS NULL;
UPDATE jobs SET failure_reason=CASE status
    WHEN 'exhausted' THEN 'attempts_exhausted'
    WHEN 'approval_expired' THEN 'approval_expired'
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'cancelled_unknown' THEN 'unknown_effect'
    WHEN 'reconcile' THEN 'unknown_effect'
    WHEN 'retry' THEN 'execution_retry'
    ELSE 'none' END;

-- Heartbeats do not invalidate a human's inspection; execution/control changes do.
CREATE FUNCTION revise_worker_control() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.status, NEW.recovery, NEW.attempt, NEW.ownership_token, NEW.failure_reason,
        NEW.crash_count, NEW.effect_pending, NEW.wait_expiry, NEW.approval_fingerprint,
        NEW.prepared_token, NEW.released_token)
       IS DISTINCT FROM
       (OLD.status, OLD.recovery, OLD.attempt, OLD.ownership_token, OLD.failure_reason,
        OLD.crash_count, OLD.effect_pending, OLD.wait_expiry, OLD.approval_fingerprint,
        OLD.prepared_token, OLD.released_token) THEN
        NEW.control_revision := OLD.control_revision + 1;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER worker_control_revision BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION revise_worker_control();
CREATE INDEX worker_reconcile_due ON jobs (operation, due_at) WHERE status='reconcile';

-- The journal contains fixed decision codes and verified identities, never arguments/errors.
CREATE TABLE worker_controls (
    tenant_id text NOT NULL, request_id text NOT NULL, actor_id text NOT NULL,
    conversation_id text NOT NULL, run_id text NOT NULL, job_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('retry','reconcile')),
    fingerprint text NOT NULL,
    decision text NOT NULL CHECK (decision IN (
        'accepted','access_denied','approval_denied','handler_unavailable',
        'ineligible','stale','replay_denied'
    )),
    response jsonb NOT NULL CHECK (octet_length(response::text) <= 4096),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, request_id),
    FOREIGN KEY (tenant_id, conversation_id, job_id) REFERENCES jobs
);
