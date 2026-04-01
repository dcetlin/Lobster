-- Migration 0006: add github_synced_at column to uow_registry
--
-- Tracks whether a completed (done) UoW has had its source GitHub issue
-- closed with a summary comment. NULL = not yet synced. Non-NULL = ISO
-- timestamp of when the sync was performed.
--
-- The post-completion sync sweep reads done UoWs where github_synced_at
-- IS NULL and issue_url IS NOT NULL, closes the issue, posts a summary
-- comment, and then sets github_synced_at = now().
--
-- This column is intentionally excluded from executor_uow_view because
-- Executor subagents have no business reading or writing it.

ALTER TABLE uow_registry ADD COLUMN github_synced_at TEXT DEFAULT NULL;
