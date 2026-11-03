-- Stable control identities resolve lost acknowledgements without consuming a wait twice.
ALTER TABLE jobs ADD COLUMN control_fingerprint text;
ALTER TABLE jobs ADD COLUMN approval_fingerprint text;
CREATE INDEX worker_owner_lookup ON jobs (lease_owner) WHERE lease_owner IS NOT NULL;
CREATE INDEX worker_tenant_capacity ON jobs (operation, tenant_id, lease_expiry)
    WHERE status = 'leased';
