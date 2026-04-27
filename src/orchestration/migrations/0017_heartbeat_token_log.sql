-- Migration 0017: per-heartbeat token snapshot log for stuck-agent detection (issue #994).
--
-- Problem:
--   PR #993 protects heartbeating agents from the startup_sweep kill gate.
--   This is correct for legitimate long-running work, but also protects stuck
--   agents that emit heartbeats while making no forward progress.
--   A stuck agent loops or spins while writing heartbeats every 60–90s.
--   The system cannot distinguish a stuck agent from a slow-but-progressing one.
--
-- Fix:
--   Agents report their cumulative token count at each heartbeat call (optional,
--   backwards compatible). This file adds:
--
--   uow_heartbeat_log — per-heartbeat snapshot table.
--     uow_id       TEXT NOT NULL — foreign key to uow_registry.id
--     recorded_at  TEXT NOT NULL — ISO UTC timestamp of this heartbeat
--     token_usage  INTEGER NULL  — cumulative token count reported by agent;
--                                  NULL if agent did not report tokens
--
--   The steward's stuck-agent detector (Phase 2c) queries this table for
--   consecutive heartbeats with zero token delta and flags them as
--   stuck_heartbeat in the log output.
--
-- Backward compatibility:
--   token_usage is optional. Agents that do not pass it continue to work.
--   The steward only flags stuck_heartbeat when token_usage IS NOT NULL for
--   two or more consecutive heartbeats — it never flags on NULL rows alone.
--
-- No FK constraint on uow_id: the registry uses SQLite without enforced FKs
--   (PRAGMA foreign_keys is not enabled by default). Cascade delete of
--   heartbeat rows when the UoW is deleted is handled by application code.

CREATE TABLE IF NOT EXISTS uow_heartbeat_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uow_id       TEXT    NOT NULL,
    recorded_at  TEXT    NOT NULL,
    token_usage  INTEGER NULL
);

CREATE INDEX IF NOT EXISTS idx_heartbeat_log_uow_recorded
    ON uow_heartbeat_log (uow_id, recorded_at);
