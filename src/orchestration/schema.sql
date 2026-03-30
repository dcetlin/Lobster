-- WOS Phase 2 — Authoritative schema for the Unit-of-Work registry.
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
--
-- Column visibility contract (Phase 2 standing convention):
--   Every column must declare its executor visibility:
--   - Executor-accessible: included in executor_uow_view.
--   - Steward-private or system-only: explicitly excluded, with a comment.
--   Run scripts/migrate_add_steward_fields.py to apply Phase 2 fields
--   to existing databases.

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
    success_criteria    TEXT    NULL,
    prescribed_skills   TEXT    NULL,
    steward_cycles      INTEGER NOT NULL DEFAULT 0,
    timeout_at          TEXT    NULL,
    estimated_runtime   INTEGER NULL,

    -- Phase 2 fields — Steward-private (excluded from executor_uow_view)
    -- steward_agenda: Steward writes its forward forecast here.
    --   Executor must never read this. Excluded from executor_uow_view.
    steward_agenda      TEXT    NULL,
    -- steward_log: Steward writes decision-point log here.
    --   Executor must never read this. Excluded from executor_uow_view.
    steward_log         TEXT    NULL,

    UNIQUE(source_issue_number, sweep_date)
);
-- vision_ref: JSON {layer, field, statement, anchored_at}
-- NULL = created before Vision Object existed or no vision anchor found.
-- Example: {"layer": "current_focus", "field": "primary",
--           "statement": "Design and commit Vision Object...",
--           "anchored_at": "2026-03-27T00:00:00+00:00"}
--
-- workflow_artifact: absolute path to workflow artifact JSON written by Steward.
-- success_criteria: prose completion statement; written at germination, immutable.
-- prescribed_skills: JSON array of skill IDs to load at Executor task start.
--   NULL = not yet prescribed; [] = explicitly prescribed with no skills.
-- steward_cycles: count of Steward diagnosis+prescription cycles completed.
-- timeout_at: ISO timestamp computed as started_at + estimated_runtime (or +1800s).
-- estimated_runtime: optional seconds estimate for timeout_at computation.

CREATE VIEW IF NOT EXISTS executor_uow_view AS
SELECT
    id, status, output_ref, started_at, completed_at,
    source_issue_number, summary,
    workflow_artifact, success_criteria, prescribed_skills,
    steward_cycles, timeout_at, estimated_runtime
FROM uow_registry;
-- steward_agenda: Steward-private, excluded from executor_uow_view.
--   Steward writes forward forecast here; Executor must never read it.
-- steward_log: Steward-private, excluded from executor_uow_view.
--   Steward writes decision-point log here; Executor must never read it.

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
