-- Migration 0015: add token_usage field for per-UoW cost telemetry (issue #990).
--
-- Problem:
--   WOS UoWs have outcome_category (heat/shit/seed/pearl) since PR #759, but lack
--   per-UoW token metering. Without this we cannot track cost per UoW, identify
--   expensive outliers, or build cost-awareness into orchestration decisions.
--
-- Fix:
--   Add token_usage INTEGER NULL to uow_registry.
--   Written by the write_result handler when a subagent reports token consumption.
--   wall_clock_seconds is a derived field (completed_at - started_at delta),
--   not stored — computed from existing timestamps at query time.
--
-- Backward compatibility:
--   Existing UoWs get token_usage=NULL (not available before this migration).
--   write_result accepts token_usage as optional; omitting it leaves the field NULL.

ALTER TABLE uow_registry ADD COLUMN token_usage INTEGER NULL;
