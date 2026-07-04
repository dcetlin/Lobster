-- Migration 0019: Add chain primitive fields for V2 multi-agent dispatch patterns.
--
-- Problem:
--   The executor currently dispatches only a single subagent per UoW.
--   V2 chain primitives (fan_out, diverge_converge, sub_uow) require:
--   - A chain_type tag so the executor knows which dispatch pattern to use.
--   - A parent_uow_id link so child UoWs can report back to their parent.
--
-- Fix:
--   Add chain_type TEXT NULL (defaults to "single") to uow_registry.
--   Add parent_uow_id TEXT NULL (NULL for top-level UoWs) to uow_registry.
--
-- chain_type values: "single" | "fan_out" | "diverge_converge" | "sub_uow"
--   "single" — current default behavior (one subagent dispatch per UoW)
--   "fan_out" — dispatch N subagents in parallel, merge results
--   "diverge_converge" — dispatch N diverge agents, then one converge agent
--   "sub_uow" — spawn child UoWs; parent waits for all children
--
-- parent_uow_id references uow_registry.id (no FK constraint for SQLite compat).
--
-- Backward compatibility:
--   Existing UoWs get chain_type = NULL (treated as "single" by executor).
--   parent_uow_id = NULL for all existing UoWs (top-level).

ALTER TABLE uow_registry ADD COLUMN chain_type TEXT NULL;
ALTER TABLE uow_registry ADD COLUMN parent_uow_id TEXT NULL;
