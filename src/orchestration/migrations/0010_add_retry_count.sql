-- Migration 0010: add retry_count field for steward re-dispatch escalation.
--
-- Problem:
--   UoWs stuck in re-dispatch loops have no upper bound on retry count.
--   The steward prescribes repeatedly without ever escalating to human review.
--   Three UoWs (uow_20260422_3cc6ca, uow_20260422_c0a82e, uow_20260422_654519)
--   are currently stuck with no resolution path.
--
-- Fix:
--   Add retry_count INTEGER NOT NULL DEFAULT 0 to uow_registry.
--   The steward increments this on each re-dispatch and caps at MAX_RETRIES=3.
--   When retry_count >= MAX_RETRIES, the UoW transitions to needs-human-review
--   instead of being re-dispatched.
--
-- Backward compatibility:
--   Existing UoWs get retry_count=0 by default. The next steward re-dispatch
--   will start incrementing from 0.

ALTER TABLE uow_registry ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
