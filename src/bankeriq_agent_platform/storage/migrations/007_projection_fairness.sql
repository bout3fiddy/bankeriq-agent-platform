-- Prioritise unmaterialised history during a Redis outage. Rotate busy
-- conversations by their last completed batch rather than their first dirty time.
CREATE INDEX projection_batch_order ON projection_work
    ((dirty_cursor > projected_cursor) DESC, COALESCE(materialized_at, dirty_since),
     tenant_id, conversation_id)
    WHERE dirty_cursor > projected_cursor OR dirty_cursor > cache_cursor;
