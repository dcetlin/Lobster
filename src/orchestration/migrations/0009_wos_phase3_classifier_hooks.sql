-- Migration 0009: WOS Phase 3 — classifier and hook system fields.
--
-- Adds columns required by the Phase 3 routing classifier and hook system:
--   hooks_frozen      — BOOL: set by loop-guard when the same hook fires >= 3x;
--                       when True, apply_hooks() skips all hook evaluation.
--   retry_count       — INT: incremented by retry-on-failure hook; stops re-queueing
--                       when >= 3.
--   classifier_thrash — BOOL: set when route_reason changes more than once for a UoW;
--                       flags instability without blocking execution.
--   rule_name         — TEXT: the classifier rule name that last fired for this UoW.
--
-- hooks_applied already exists in schema.sql (TEXT DEFAULT '[]'); no change needed.
-- route_reason already exists; classifier_thrash detection reads it.

ALTER TABLE uow_registry ADD COLUMN hooks_frozen     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE uow_registry ADD COLUMN retry_count      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE uow_registry ADD COLUMN classifier_thrash INTEGER NOT NULL DEFAULT 0;
ALTER TABLE uow_registry ADD COLUMN rule_name        TEXT    DEFAULT NULL;
