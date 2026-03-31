-- Migration 0005: add issue_url field to uow_registry
-- Stores the canonical GitHub issue URL so UoWs are self-describing
-- and the Steward/Executor no longer need to hardcode the repo to
-- reconstruct the URL. Populated at proposal time from source_issue_number
-- + source_repo (or explicitly provided). NULL for pre-existing rows.

ALTER TABLE uow_registry ADD COLUMN issue_url TEXT DEFAULT NULL;
