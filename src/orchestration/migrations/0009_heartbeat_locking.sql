-- Migration 0009: heartbeat-based UoW locking fields.
--
-- Problem:
--   The fixed 4h TTL in recover_ttl_exceeded_uows() fires based on elapsed time
--   since started_at, regardless of whether the executing agent is still alive.
--   An agent that crashes at minute 1 is invisible to the system for 3h59m.
--   At scale, this means duplicate execution of non-idempotent UoWs and wasted quota.
--
-- Fix:
--   Add two fields to uow_registry:
--
--   heartbeat_at  TEXT     -- ISO timestamp; written by executing agent periodically.
--                          -- NULL until first heartbeat write.
--   heartbeat_ttl INTEGER  -- Maximum seconds of silence before steward treats UoW
--                          -- as stalled. Set at claim time. Default: 300 (5 minutes).
--
--   The observation loop (steward-heartbeat.py) checks heartbeat_at:
--   - If heartbeat_at is non-NULL and (now - heartbeat_at) > heartbeat_ttl: stall detected.
--   - If heartbeat_at is NULL: falls back to started_at-based TTL (backward compatibility).
--
-- Backward compatibility:
--   UoWs created before this migration have heartbeat_at=NULL and heartbeat_ttl=300.
--   The observation loop falls back to started_at when heartbeat_at is NULL.
--   No behavior change for legacy UoWs until the first agent writes a heartbeat.

ALTER TABLE uow_registry ADD COLUMN heartbeat_at TEXT;
ALTER TABLE uow_registry ADD COLUMN heartbeat_ttl INTEGER DEFAULT 300;
