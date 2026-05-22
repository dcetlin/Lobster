-- Migration 0025: replace TTL recovery with visibility-timeout (claimed_until)
-- WOS-UoW: uow_20260519_5f17b8
ALTER TABLE uow_registry ADD COLUMN claimed_until TEXT NULL;
