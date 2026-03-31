-- Add status filter to executor_uow_view so Executor only sees ready-for-executor UoWs.
-- Without this filter, UoWs in any status were visible to the Executor, defeating
-- the read-path isolation contract. After this migration, only UoWs in
-- 'ready-for-executor' status are returned by SELECT on executor_uow_view.
DROP VIEW IF EXISTS executor_uow_view;
CREATE VIEW executor_uow_view AS
SELECT
    id, status, output_ref, started_at, completed_at,
    source_issue_number, summary,
    workflow_artifact, success_criteria, prescribed_skills,
    steward_cycles, timeout_at, estimated_runtime
FROM uow_registry
WHERE status = 'ready-for-executor';
