-- Migration 0024: route dispatch_eligibility_skip to a dedicated table.
--
-- The audit_log table accumulates dispatch_eligibility_skip records at ~9:1
-- noise-to-signal ratio, burying real forensic events. This migration creates
-- a separate dispatch_skip_log table for these non-decision records, keeping
-- audit_log clean for actions and transitions.

CREATE TABLE IF NOT EXISTS dispatch_skip_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    uow_id         TEXT    NOT NULL,
    eligibility    TEXT    NOT NULL,
    steward_cycles INTEGER,
    actor          TEXT,
    note           TEXT
);
