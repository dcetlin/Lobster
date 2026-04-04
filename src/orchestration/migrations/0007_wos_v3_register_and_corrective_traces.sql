-- Migration 0007: WOS V3 — register field, uow_mode field, corrective_traces table,
--                 and delivery≠closure fields (closed_at, close_reason)
--
-- Design:
--   register   — attentional configuration required for completion evaluation.
--                Immutable after germination. Written at INSERT time by the Germinator.
--                Values: operational | iterative-convergent | philosophical | human-judgment
--                Default: 'operational' (safe default for existing rows).
--
--   uow_mode   — mirrors register; used for execution context selection by the Executor.
--                Kept as a separate column to allow future divergence between routing
--                register and execution mode without a schema change.
--
--   closed_at  — ISO timestamp written when the Steward declares done (loop closure).
--                Distinct from completed_at (executor delivery). NULL until Steward closes.
--                This enforces the delivery≠closure distinction: result.json written by
--                the Executor marks delivery; Steward writing closed_at marks closure.
--
--   close_reason — Prose explaining the Steward's closure decision. Required when
--                  transitioning to done. Enables post-hoc audit of why a loop was
--                  declared closed.
--
-- corrective_traces table:
--   Every executor return writes a structured trace capturing execution summary,
--   surprises, prescription delta, and gate score. Traces accumulate in the garden.
--   The Steward reads them at diagnosis time for register-aware orientation.
--   Absence of a trace is logged as a contract violation but does not block Steward
--   re-entry (unlike result.json absence, which blocks completion declaration).
--
-- Note on executor_uow_view: register and uow_mode are included in the view so that
-- the Executor can select a register-appropriate execution context. closed_at and
-- close_reason are excluded (Steward-private closure fields).

ALTER TABLE uow_registry ADD COLUMN register TEXT NOT NULL DEFAULT 'operational';
ALTER TABLE uow_registry ADD COLUMN uow_mode TEXT;
ALTER TABLE uow_registry ADD COLUMN closed_at TEXT DEFAULT NULL;
ALTER TABLE uow_registry ADD COLUMN close_reason TEXT DEFAULT NULL;

-- Backfill uow_mode to match register for all existing rows.
UPDATE uow_registry SET uow_mode = register WHERE uow_mode IS NULL;

-- Drop and recreate executor_uow_view to include register and uow_mode.
-- SQLite does not support ALTER VIEW, so we must DROP and CREATE.
DROP VIEW IF EXISTS executor_uow_view;

CREATE VIEW IF NOT EXISTS executor_uow_view AS
SELECT
    id, status, output_ref, started_at, completed_at,
    source_issue_number, summary,
    workflow_artifact, success_criteria, prescribed_skills,
    steward_cycles, timeout_at, estimated_runtime,
    register, uow_mode
FROM uow_registry;

-- corrective_traces: learning artifacts written by every executor return.
-- Accumulated in the garden; read by Steward at diagnosis time.
CREATE TABLE IF NOT EXISTS corrective_traces (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    uow_id              TEXT    NOT NULL,
    register            TEXT    NOT NULL,
    execution_summary   TEXT    NOT NULL,
    surprises           TEXT,   -- JSON array of unexpected findings
    prescription_delta  TEXT,   -- what would change the prescription on next run
    gate_score          TEXT,   -- JSON: {"command": "...", "result": "...", "score": 0.9} or null
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Index for efficient Steward lookup by uow_id at diagnosis time.
CREATE INDEX IF NOT EXISTS idx_corrective_traces_uow_id
    ON corrective_traces(uow_id);
