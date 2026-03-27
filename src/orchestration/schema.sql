-- WOS Phase 1 — Authoritative schema for the Unit-of-Work registry.
--
-- This file is the source of truth for all DDL. registry.py loads and
-- applies it at init time via conn.executescript(). Do not duplicate
-- these CREATE TABLE statements elsewhere.
--
-- Key constraints:
--   uow_registry.UNIQUE(source_issue_number, sweep_date)
--       DB-level dedup gate: one UoW per (issue, sweep_date) pair.
--       Cross-sweep-date dedup is enforced in Python (pre-write decision
--       table in Registry.upsert).
--
--   Non-terminal statuses: proposed, pending, active, blocked
--       While a record in any of these states exists for an issue,
--       re-proposals are skipped (the issue is already in flight).
--
--   Terminal statuses: done, failed, expired
--       A terminal record allows re-proposal for the same issue on a
--       future sweep date.
--
--   INSERT OR REPLACE is explicitly not used — it would silently discard
--   execution state already recorded on an existing row.

CREATE TABLE IF NOT EXISTS uow_registry (
    id                  TEXT    PRIMARY KEY,
    type                TEXT    NOT NULL DEFAULT 'executable',
    source              TEXT    NOT NULL,
    source_issue_number INTEGER,
    sweep_date          TEXT,
    status              TEXT    NOT NULL DEFAULT 'proposed',
    posture             TEXT    NOT NULL DEFAULT 'solo',
    agent               TEXT,
    children            TEXT    DEFAULT '[]',
    parent              TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    summary             TEXT    NOT NULL,
    output_ref          TEXT,
    hooks_applied       TEXT    DEFAULT '[]',
    route_reason        TEXT,
    route_evidence      TEXT    DEFAULT '{}',
    trigger             TEXT    DEFAULT '{"type": "immediate"}',
    UNIQUE(source_issue_number, sweep_date)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    uow_id      TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    agent       TEXT,
    note        TEXT
);
