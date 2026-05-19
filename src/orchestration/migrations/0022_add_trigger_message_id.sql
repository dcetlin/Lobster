-- Migration 0022: add trigger_message_id to uow_registry (issue #1108).
--
-- Purpose:
--   trigger_message_id is the critical join key linking a Telegram inbox
--   message to the UoW it spawned. With this column, "which conversations
--   led to pearls?" becomes a single JOIN query rather than a manual
--   log-scraping exercise.
--
-- Design:
--   - NULL for UoWs created by the automated cultivator (GitHub-sweep path)
--     or registry_cli commands — those have no associated Telegram message.
--   - Populated only when a UoW is created from a user-initiated Telegram
--     conversation (future path; the column is wired at the registry level
--     so callers can pass it when they have it).
--   - TEXT to match inbox message_id format (e.g. "1778365681821_8563").
--   - No foreign key — inbox messages live in a separate SQLite DB.

ALTER TABLE uow_registry ADD COLUMN trigger_message_id TEXT NULL;
