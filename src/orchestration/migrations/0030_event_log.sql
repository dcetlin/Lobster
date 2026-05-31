-- Migration 0030: Create event_log table for WOS Event-Native Nervous System.
--
-- The event_log records typed events emitted by the WOS event pipeline:
--   wos_issue_created    — emitted when a GitHub issue with label wos:uow is created
--   wos_uow_completed    — emitted when a UoW transitions to completed/failed state
--   wos_capacity_available — emitted when executor has slots free (running < max_parallel)
--
-- Events are written on emission and marked consumed (consumed_at non-null)
-- after the dispatcher processes the corresponding inbox message.
--
-- Deduplication: (event_type, dedup_key) is UNIQUE so the delta poller can
-- re-run safely without flooding the inbox with duplicate events.
-- dedup_key is event-type-specific (e.g. issue number for wos_issue_created,
-- uow_id for wos_uow_completed, freed_uow_id for wos_capacity_available).
--
-- Retention: consumed events older than 30 days may be pruned.
-- The poller reads max(emitted_at) as a since-cursor to find the next delta.

CREATE TABLE IF NOT EXISTS event_log (
    event_id          TEXT    PRIMARY KEY,           -- UUID
    event_type        TEXT    NOT NULL,               -- wos_issue_created | wos_uow_completed | wos_capacity_available
    payload           TEXT    NOT NULL DEFAULT '{}',  -- JSON
    emitted_at        TEXT    NOT NULL,               -- ISO-8601 UTC
    consumed_at       TEXT    DEFAULT NULL,           -- ISO-8601 UTC; NULL = not yet consumed
    consumer_task_id  TEXT    DEFAULT NULL,           -- task_id of the consuming dispatcher job
    dedup_key         TEXT    DEFAULT NULL            -- type-specific dedup handle; see UNIQUE constraint below
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_dedup
    ON event_log (event_type, dedup_key)
    WHERE dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_log_emitted_at
    ON event_log (emitted_at);

CREATE INDEX IF NOT EXISTS idx_event_log_unconsumed
    ON event_log (event_type, emitted_at)
    WHERE consumed_at IS NULL;
