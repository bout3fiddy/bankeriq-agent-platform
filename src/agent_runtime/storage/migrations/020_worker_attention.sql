-- Active stopped work needs continuing operator attention even after its last
-- error event leaves the alert window. Keep passive probes independent of the
-- growing completed-job history; this index changes no execution/recovery state.
CREATE INDEX worker_stopped_attention ON jobs (operation, due_at, job_id)
    WHERE recovery IS NOT NULL AND status='stopped';
