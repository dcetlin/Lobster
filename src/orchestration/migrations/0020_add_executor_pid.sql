-- Migration 0020: add executor_pid column for subprocess PID tracking.
--
-- Problem:
--   The WOS executor dispatches UoWs via subprocess.Popen(..., start_new_session=True).
--   Once dispatched, there is no way to kill the subprocess explicitly because
--   the PID is not stored. The 'wos abort <uow_id>' command needs a stored PID
--   to send SIGTERM to the process group.
--
-- Fix:
--   Add executor_pid INTEGER NULL to uow_registry.
--   Written at dispatch time by the Executor after Popen(...) succeeds.
--   Cleared when the UoW completes or when the abort command kills the process.
--   Also cleared during orphan recovery (steward/heartbeat orphan path).
--
-- Backward compatibility:
--   Existing UoWs get executor_pid = NULL (no PID stored). The kill path
--   treats NULL as "no running process found" and returns False.

ALTER TABLE uow_registry ADD COLUMN executor_pid INTEGER NULL;
