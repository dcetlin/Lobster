-- Migration 0031: Add checkpoint_ref column to uow_registry.
--
-- checkpoint_ref: path to the most recently written checkpoint.json for this UoW.
--   Written by the executor checkpoint protocol (checkpoint.py).
--   NULL until a checkpoint is written. Infrastructure only — no gating logic.
ALTER TABLE uow_registry ADD COLUMN checkpoint_ref TEXT DEFAULT NULL;
