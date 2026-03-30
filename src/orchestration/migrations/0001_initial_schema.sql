-- Migration 0001: Initial WOS schema
--
-- Establishes the baseline schema for the Unit-of-Work registry.
-- This migration captures the full schema as it existed at the point
-- the migration system was introduced. New installs run all migrations
-- sequentially; existing installs with CREATE TABLE IF NOT EXISTS will
-- skip DDL that already exists but still record this migration as applied.
--
-- Tables created:
--   uow_registry — core UoW store with all Phase 2 fields
--   executor_uow_view — view exposing only executor-accessible columns
--   audit_log — append-only event log for status transitions

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
    vision_ref          TEXT    DEFAULT NULL,

    -- Phase 2 fields — Executor-accessible (included in executor_uow_view)
    workflow_artifact   TEXT    NULL,
    success_criteria    TEXT    NOT NULL DEFAULT '',
    prescribed_skills   TEXT    NULL,
    steward_cycles      INTEGER NOT NULL DEFAULT 0,
    timeout_at          TEXT    NULL,
    estimated_runtime   INTEGER NULL,

    -- Phase 2 fields — Steward-private (excluded from executor_uow_view)
    steward_agenda      TEXT    NULL,
    steward_log         TEXT    NULL,

    UNIQUE(source_issue_number, sweep_date)
);

CREATE VIEW IF NOT EXISTS executor_uow_view AS
SELECT
    id, status, output_ref, started_at, completed_at,
    source_issue_number, summary,
    workflow_artifact, success_criteria, prescribed_skills,
    steward_cycles, timeout_at, estimated_runtime
FROM uow_registry;

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
