ALTER TABLE prompt_versions ADD COLUMN source_manifest jsonb;
ALTER TABLE prompt_versions ADD CONSTRAINT bounded_prompt_source_manifest
    CHECK (source_manifest IS NULL OR octet_length(source_manifest::text) <= 65536);
