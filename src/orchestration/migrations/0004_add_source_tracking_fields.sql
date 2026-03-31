-- Migration 0004: add source tracking fields for GardenCaretaker
-- source_ref: canonical SourceRef string ("github:issue/42")
-- source_last_seen_at: ISO 8601 timestamp of last successful source.get_issue()
-- source_state: last known state from source ("open", "closed", "deleted", NULL=unknown)

ALTER TABLE uow_registry ADD COLUMN source_ref TEXT;
ALTER TABLE uow_registry ADD COLUMN source_last_seen_at TEXT;
ALTER TABLE uow_registry ADD COLUMN source_state TEXT;

-- Index for GardenCaretaker's tend() — fast lookup of UoWs by source_ref
CREATE INDEX IF NOT EXISTS idx_uow_source_ref ON uow_registry(source_ref);
