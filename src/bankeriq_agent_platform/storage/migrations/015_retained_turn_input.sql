-- A within-turn prefix covers completed exchanges while retaining the original
-- user input verbatim. Record that exception to prefix coverage explicitly.
ALTER TABLE semantic_summaries ADD COLUMN retained_cursor bigint NOT NULL DEFAULT 0;
ALTER TABLE semantic_operations ADD COLUMN retained_cursor bigint NOT NULL DEFAULT 0;
ALTER TABLE semantic_summaries ADD CHECK (retained_cursor >= 0);
ALTER TABLE semantic_operations ADD CHECK (retained_cursor >= 0);
