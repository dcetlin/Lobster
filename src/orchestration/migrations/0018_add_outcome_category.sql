-- Migration 0016: add outcome_category column for metabolic taxonomy storage (issue #998).
--
-- Problem:
--   outcome_category (heat/shit/seed/pearl) is written at write_result time (live since
--   PR #759) but is persisted only to an append-only JSONL ledger, not to uow_registry.
--   This means it is not DB-queryable: registry_cli report (PR #997) cannot surface it
--   in the per-UoW listing or the summary breakdown, and any query correlating outcome
--   classification with other UoW fields requires joining an external flat file.
--
-- Fix:
--   Add outcome_category TEXT NULL to uow_registry.
--   Written by complete_uow when the write_result handler confirms the subagent's
--   outcome_category is valid (one of: heat, shit, seed, pearl).
--
-- Backward compatibility:
--   Existing UoWs get outcome_category = NULL (value was not captured before this
--   migration). The column is nullable throughout. write_result accepts outcome_category
--   as optional; omitting it leaves the field NULL.

ALTER TABLE uow_registry ADD COLUMN outcome_category TEXT NULL;
