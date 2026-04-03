# Agent Cleanup Improvements

## Overview

This document describes the agent cleanup improvements that prevent agent backlog accumulation in the WOS (Work Orchestration System).

## Problem Statement

Previously, agents dispatched by the Executor would remain registered in the `agent_sessions` table indefinitely, even after they completed their work. This caused agent backlog to accumulate—the 28-agent backlog observed in the system was a direct result of missing cleanup hooks after successful UoW dispatch.

Root cause: The Executor called `execute_uow()`, which dispatched the functional-engineer subagent, but had no mechanism to unregister the agent once the work was complete.

## Solution Overview

Two complementary mechanisms prevent backlog:

### 1. Executor Cleanup Handler (HIGH PRIORITY)

**Location:** `scheduled-tasks/executor-heartbeat.py::run_executor_cycle()`

**Mechanism:**
- After `executor.execute_uow(uow_id)` completes successfully, the Executor calls `session_end()` to unregister the agent.
- The call includes:
  - `id_or_task_id`: The executor_id returned by the dispatch
  - `status`: Set to "completed" for successful dispatch
  - `result_summary`: Brief context (e.g., "UoW 123 dispatched successfully")

**Code location:**
```python
result = executor.execute_uow(uow_id)
dispatched += 1

# Cleanup: unregister the agent after successful dispatch
if result.executor_id:
    try:
        from src.agents.session_store import session_end
        session_end(
            id_or_task_id=result.executor_id,
            status="completed",
            result_summary=f"UoW {uow_id} dispatched successfully",
        )
    except Exception as cleanup_err:
        log.warning("Failed to unregister agent %s — %s", result.executor_id, cleanup_err)
```

**Impact:**
- Prevents the "completed but still registered" state that caused backlog
- Runs on every executor heartbeat (every 3 minutes)
- Non-fatal: if cleanup fails, the UoW still completes and is returned to the Steward

**Testing:**
- Verify `session_end()` is called with the correct executor_id
- Verify cleanup failures are logged but don't block UoW processing
- Verify agents are actually unregistered by querying the `agent_sessions` table

### 2. Automatic Stale Agent Cleanup (MEDIUM PRIORITY)

**Location:** `scheduled-tasks/steward-heartbeat.py::run_stale_agent_cleanup()`

**Mechanism:**
- Runs as Phase 0 of the steward heartbeat (before startup sweep)
- Scans `agent_sessions` table for agents in 'running' state
- Identifies agents older than `STALE_AGENT_THRESHOLD_SECONDS` (2 hours)
- For agents with `output_file` specified, checks if the file has been recently updated
  - If file was updated within the threshold, the agent may still be active—skip it
  - If file is stale or missing, the agent is presumed dead—unregister it
- Calls `session_end()` with status="dead" for each stale agent

**Code location (Phase 0):**
```python
# Phase 0: Stale agent cleanup
log.info("--- Phase 0: Stale agent cleanup ---")
cleanup_result = run_stale_agent_cleanup(dry_run=dry_run)
log.info(
    "Stale agent cleanup complete: evaluated=%d cleaned=%d skipped=%d (running_agents=%d)",
    cleanup_result["evaluated"],
    cleanup_result["cleaned"],
    cleanup_result["skipped"],
    cleanup_result["running_total"],
)
```

**Impact:**
- Acts as a safety net for agents that weren't cleaned up by the executor handler
- Prevents indefinite accumulation of stuck agents
- Gracefully skips agents that still show activity
- Runs every 3 minutes on heartbeat

**Testing:**
- Create mock agent with 3-hour-old spawned_at timestamp
- Verify it's identified and unregistered in non-dry-run mode
- Verify dry-run mode counts but doesn't unregister
- Verify recent agents are skipped

### 3. Agent Metrics in Steward Output (LOW PRIORITY)

**Location:** `scheduled-tasks/steward-heartbeat.py::main()` and `run_stale_agent_cleanup()`

**Mechanism:**
- `run_stale_agent_cleanup()` queries the total running agent count via:
  ```sql
  SELECT COUNT(*) FROM agent_sessions WHERE status = 'running'
  ```
- Returns `running_total` in the cleanup result dictionary
- Logged at INFO level each heartbeat cycle:
  ```
  Stale agent cleanup complete: evaluated=0 cleaned=0 skipped=0 (running_agents=5)
  ```

**Impact:**
- Makes agent backlog visible in normal operations
- Operators can spot backlog trends without querying the database directly
- Helps diagnose whether cleanup is working by observing the running_agents count over time

**Example log output:**
```
Stale agent cleanup complete: evaluated=2 cleaned=2 skipped=0 (running_agents=3)
```
This shows: 2 stale agents evaluated, both cleaned up, 0 skipped, 3 running agents remain.

## Configuration

### Stale Agent Threshold

The threshold for considering an agent "stale" is configurable via:

```python
STALE_AGENT_THRESHOLD_SECONDS: int = 7200  # 2 hours
```

To adjust:
1. Edit `scheduled-tasks/steward-heartbeat.py`
2. Modify `STALE_AGENT_THRESHOLD_SECONDS` to desired seconds
3. Restart the steward heartbeat

### Dry-Run Mode

Both cleanup mechanisms support `--dry-run` mode:

```bash
# Executor cleanup (dry-run)
uv run scheduled-tasks/executor-heartbeat.py --dry-run

# Steward cleanup (dry-run)
uv run scheduled-tasks/steward-heartbeat.py --dry-run
```

In dry-run mode:
- Agents are queried but NOT unregistered
- All logging proceeds normally
- Skipped counts are incremented instead of cleaned counts

## Monitoring and Observability

### Metrics to Monitor

1. **Executor cleanup metrics** (every 3 min):
   - Check logs for `"Executor cycle: unregistered agent"`
   - Track cleanup failures: `"failed to unregister agent"`

2. **Steward cleanup metrics** (every 3 min):
   - `evaluated`: Stale agents identified in this cycle
   - `cleaned`: Agents successfully unregistered
   - `skipped`: Agents skipped due to recent activity
   - `running_agents`: Total agents still in 'running' status

3. **Agent backlog health**:
   - Query agent_sessions: `SELECT COUNT(*) FROM agent_sessions WHERE status = 'running'`
   - Track over time—should trend downward if cleanup is effective
   - Compare to expected count from simultaneous UoW dispatch

### Example Queries

Check total running agents:
```bash
sqlite3 ~/messages/config/agent_sessions.db \
  'SELECT COUNT(*) FROM agent_sessions WHERE status="running"'
```

Find old agents:
```bash
sqlite3 ~/messages/config/agent_sessions.db \
  'SELECT id, spawned_at FROM agent_sessions WHERE status="running" AND spawned_at < datetime("now", "-2 hours")'
```

## Testing

Tests are located in `tests/unit/test_agent_cleanup.py`:

1. **Executor cleanup tests**:
   - `test_executor_cleanup_calls_session_end_on_dispatch`: Verify session_end is called
   - `test_executor_cleanup_handles_missing_executor_id`: Verify graceful handling of missing IDs

2. **Stale agent cleanup tests**:
   - `test_stale_agent_cleanup_identifies_old_agents`: Verify old agents are identified
   - `test_stale_agent_cleanup_dry_run`: Verify dry-run mode works
   - `test_agent_metrics_included_in_result`: Verify metrics are captured

Run tests:
```bash
cd ~/lobster && uv run pytest tests/unit/test_agent_cleanup.py -v
```

## Deployment

The cleanup improvements are automatically deployed when the Lobster system is installed or upgraded.

### Installation

During `install.sh`:
- Executor-heartbeat.py is updated with the cleanup handler
- Steward-heartbeat.py is updated with the cleanup phase
- Tests are included in the test suite

### Upgrade

If upgrading from an older version:
1. The improvements are applied automatically via the upgrade script
2. No manual intervention required
3. The new Phase 0 cleanup phase will start running on the next steward heartbeat

## References

- **UoW Dispatch Flow**: docs/wos-v2-design.md § Executor, § Phase 2
- **Agent Session Store**: src/agents/session_store.py (session_end API)
- **Registry API**: src/orchestration/registry.py
- **Executor Contract**: docs/executor-contract.md

## Future Improvements

Potential enhancements for future phases:

1. **Agent cleanup on write_result**: When a subagent calls `write_result()`, the dispatcher could automatically call `session_end()`. This would shift cleanup responsibility from the Executor to the subagent.

2. **Configurable thresholds**: Expose stale agent threshold as a configuration parameter in jobs.json.

3. **Agent lifecycle events**: Log agent lifecycle transitions (spawned, completed, cleaned) for better observability.

4. **Metrics export**: Export agent backlog metrics to monitoring systems (Prometheus, CloudWatch, etc.) for alerting on excessive backlog.
