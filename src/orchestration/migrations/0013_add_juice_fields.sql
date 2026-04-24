-- Migration 0013: add juice_quality and juice_rationale fields for dispatch priority.
--
-- Problem:
--   The steward has no structural signal to distinguish prescriptions with live
--   generative momentum (juice) from stalled/indeterminate threads. Without this,
--   dispatch priority is implicit LIFO (newest-first), which gives no preference
--   to threads the steward recognises as productively alive.
--
-- Fix:
--   juice_quality TEXT NULL — 'juice' when the steward asserts live momentum;
--     NULL when not asserted. Re-evaluated on every prescription cycle
--     (Option C from the spec: juice must be re-earned each cycle, not persisted
--     automatically). Indexed for efficient ORDER BY in dispatch query.
--   juice_rationale TEXT NULL — mandatory prose when juice_quality='juice'.
--     Records *what* is alive and why (the "What is the juice?" calibration).
--     NULL when juice_quality is NULL. The schema enforces this as a convention
--     rather than a DB CHECK constraint so that partial writes during migration
--     do not break existing rows.
--
-- Dispatch query impact (steward.py):
--   SELECT * FROM uow_registry WHERE status = 'ready-for-steward'
--   ORDER BY
--     CASE WHEN juice_quality = 'juice' THEN 0 ELSE 1 END ASC,
--     created_at DESC
--
-- Backward compatibility:
--   Existing UoWs get juice_quality=NULL and juice_rationale=NULL by default.
--   The steward treats NULL as non-juiced on the first post-migration cycle.

ALTER TABLE uow_registry ADD COLUMN juice_quality TEXT;
ALTER TABLE uow_registry ADD COLUMN juice_rationale TEXT;

CREATE INDEX IF NOT EXISTS idx_uow_juice_quality
    ON uow_registry (juice_quality)
    WHERE status = 'ready-for-steward';
