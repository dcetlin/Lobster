-- Migration 0029: Adaptive Steward — verdict_accumulator, prescription_hypothesis_log,
-- normalization_log tables.
--
-- §3-I Adaptive Steward (wos-evolution-spec.md).
--
-- verdict_accumulator: aggregate scored outcomes per (register, hypothesis_normalized).
--   Each row accumulates pass/fail/partial counts across all UoWs that shared the same
--   normalized hypothesis. The UNIQUE constraint on (register, diagnosis_hypothesis)
--   enforces the aggregate-upsert pattern: every new scored outcome either inserts a
--   new row or increments an existing one.
--
-- prescription_hypothesis_log: per-UoW record of the raw hypothesis at prescription time.
--   The Steward writes one row here when it logs a prescription. At UoW closure, the
--   normalization hook reads this row to find the raw hypothesis before scoring it.
--
-- normalization_log: audit trail for every Haiku normalization call.
--   Records both the original and normalized form so hypothesis drift and overcollapse
--   in normalization behavior are visible over time.

CREATE TABLE IF NOT EXISTS verdict_accumulator (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    register             TEXT    NOT NULL,
    diagnosis_hypothesis TEXT    NOT NULL,       -- normalized form
    n_successes          INTEGER NOT NULL DEFAULT 0,
    n_failures           INTEGER NOT NULL DEFAULT 0,
    n_partial            INTEGER NOT NULL DEFAULT 0,
    last_updated         TEXT    NOT NULL,
    UNIQUE(register, diagnosis_hypothesis)
);

CREATE TABLE IF NOT EXISTS prescription_hypothesis_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uow_id       TEXT    NOT NULL,
    hypothesis   TEXT    NOT NULL,
    register     TEXT    NOT NULL,
    generated_at TEXT    NOT NULL,
    outcome      TEXT    NULL,      -- 'pass' | 'fail' | 'partial' — written at scoring time
    scored_at    TEXT    NULL       -- ISO timestamp — written at scoring time
);

-- normalization_log: audit of every Haiku normalization call.
-- model: the model ID used (e.g. "claude-haiku-4-5-20251001").
CREATE TABLE IF NOT EXISTS normalization_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uow_id     TEXT    NOT NULL,
    raw        TEXT    NOT NULL,
    normalized TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    ts         TEXT    NOT NULL
);
