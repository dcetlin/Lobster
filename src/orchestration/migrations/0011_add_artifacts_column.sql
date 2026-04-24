-- Migration 0011: add artifacts column for outcome_refs extracted from write_result payloads.
--
-- Problem:
--   When a WOS subagent calls write_result, the completion text often contains
--   references to PRs, issues, and files produced during execution. These refs
--   were previously extracted only into the result.json file (via _enrich_result_file),
--   making them invisible to the registry-level steward and retrospective queries.
--
-- Fix:
--   Add artifacts TEXT NULL to uow_registry. Stores a JSON array of typed ref objects
--   extracted from the write_result payload at completion time. Schema per item:
--     {type: "pr"|"issue"|"file"|"commit", ref: str, category: str, description?: str}
--
-- Backward compatibility:
--   Existing UoWs get artifacts=NULL by default (treated as empty list by readers).
--   Populated forward-only: only UoWs completed after this migration receive refs.

ALTER TABLE uow_registry ADD COLUMN artifacts TEXT NULL;
