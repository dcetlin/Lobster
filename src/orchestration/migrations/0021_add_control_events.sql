-- Migration 0020: add control_events table for dispatcher command timeline (issue #1104).
--
-- Problem:
--   The WOS dashboard shows UoW queue state and token costs but has no timeline
--   of dispatcher-level control actions (wos start, wos stop, wos abort). These
--   events are observable individually (wos-config.json, registry executor_pid,
--   oracle/verdicts/ mtimes) but not in a unified, time-correlated view.
--
-- Fix:
--   Add a control_events table — a lightweight append-only log written directly
--   in dispatcher_handlers.py at the moment each control action executes. One row
--   per event: ts, event_type (e.g. 'wos_start', 'wos_stop', 'wos_abort'), and
--   an optional JSON payload for contextual data (uow_id, pr_number, etc.).
--
-- Why append-only:
--   Control events are facts about dispatcher decisions. They must not be updated
--   or deleted. The table is intentionally minimal — no foreign keys, no status
--   columns — so it can absorb any control event type without schema changes.
--
-- Dashboard use:
--   JOIN control_events on ts range with audit_events and token_ledger for causal
--   correlation: "abort called at T, UoW failed at T+1".

CREATE TABLE IF NOT EXISTS control_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (datetime('now')),
    event_type  TEXT    NOT NULL,
    payload     TEXT
);
