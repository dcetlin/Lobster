-- Migration 0023: create activity_timeline view (issue #1108).
--
-- Purpose:
--   Unified "what happened" timeline spanning UoW status transitions (audit_log)
--   and dispatcher control events (control_events). Enables time-correlated
--   queries like: "which wos_start event preceded this UoW's execution?", or
--   "did we see any abort events around the time this conversation's UoW ran?".
--
-- Columns:
--   ts                — ISO timestamp of the event
--   event_type        — 'uow_status_change' for audit rows; event_type value for control rows
--   entity_id         — uow_id for audit rows; CAST(control_events.id AS TEXT) for control rows
--   detail            — new_status for audit rows; payload JSON for control rows
--   outcome_category  — joined from uow_registry (NULL for control rows)
--   token_usage       — joined from uow_registry (NULL for control rows)
--   trigger_message_id — joined from uow_registry (NULL for control rows)
--
-- Note: control_events.id is an INTEGER PK (AUTOINCREMENT); cast to TEXT for
-- consistent entity_id type across the UNION.

CREATE VIEW IF NOT EXISTS activity_timeline AS
SELECT
    al.ts,
    'uow_status_change'     AS event_type,
    al.uow_id               AS entity_id,
    al.to_status            AS detail,
    u.outcome_category,
    u.token_usage,
    u.trigger_message_id
FROM audit_log al
JOIN uow_registry u ON al.uow_id = u.id
UNION ALL
SELECT
    ce.ts,
    ce.event_type,
    CAST(ce.id AS TEXT)     AS entity_id,
    ce.payload              AS detail,
    NULL                    AS outcome_category,
    NULL                    AS token_usage,
    NULL                    AS trigger_message_id
FROM control_events ce
ORDER BY ts DESC;
