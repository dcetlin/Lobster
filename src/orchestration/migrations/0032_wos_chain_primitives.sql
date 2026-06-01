-- Migration 0032: WOS chain primitives schema additions.
--
-- Adds columns required by the three chain dispatch primitives:
--   fan_out, spec_breakdown, diverge_converge.
--
-- perspectives_outputs: JSON dict keyed by perspective name, storing the
--   executor_id for each dispatched perspective subagent. NULL until a
--   fan_out chain dispatch fires for this UoW.
--   Steward-private (excluded from executor_uow_view). Queryability column
--   for fan_out UoW inspection; chain_type is carried in workflow_artifact JSON
--   and is the Executor's read path for dispatch routing.
--
-- chain_perspectives: JSON array of perspective names for fan_out UoWs.
--   Mirrors the perspectives field in WorkflowArtifact for queryability.
--   NULL for non-fan-out UoWs.
--   Steward-private (excluded from executor_uow_view).
--
-- chain_approaches: JSON array of approach names for diverge_converge UoWs.
--   NULL for non-diverge-converge UoWs.
--   Steward-private (excluded from executor_uow_view).
--
-- WOS-UoW: uow_20260601_424433

ALTER TABLE uow_registry ADD COLUMN perspectives_outputs TEXT DEFAULT NULL;
ALTER TABLE uow_registry ADD COLUMN chain_perspectives TEXT DEFAULT NULL;
ALTER TABLE uow_registry ADD COLUMN chain_approaches TEXT DEFAULT NULL;
