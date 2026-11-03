-- Keep due scans independent of completed history, including reconciliation work.
CREATE INDEX worker_eligible_scan ON jobs (operation, due_at, job_id)
    WHERE recovery IS NOT NULL AND status IN ('ready','retry','reconcile','approval');
CREATE INDEX worker_resource_holders ON jobs (tenant_id, resource_key)
    WHERE resource_key IS NOT NULL AND (effect_pending OR status='leased');
CREATE INDEX child_progress ON jobs
    (tenant_id, conversation_id, run_id, input_revision, call_position)
    WHERE group_id IS NOT NULL;
