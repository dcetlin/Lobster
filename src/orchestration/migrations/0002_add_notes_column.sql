-- Migration 0002: Add notes column to uow_registry
--
-- Adds the `notes` column for free-form annotation on UoW records.
-- This column was added to schema.sql for new installs (item 18) but
-- existing installs need this ALTER TABLE to gain the column.
--
-- The column is intentionally permissive:
--   - NOT NULL DEFAULT '{}' — empty JSON object, consistent with other
--     JSON-typed fields (route_evidence, trigger) that default to empty
--     structures rather than NULL.
--   - TEXT type — contents are JSON but SQLite has no native JSON type.
--
-- Idempotency: SQLite does not support IF NOT EXISTS on ALTER TABLE.
-- The migration runner skips this file if version 2 is already recorded
-- in _migrations, so double-application is not possible.

ALTER TABLE uow_registry ADD COLUMN notes TEXT NOT NULL DEFAULT '{}';
