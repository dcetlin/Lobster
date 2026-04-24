# WOS Hourly Observation

**Job**: wos-hourly-observation
**Schedule**: Top of each hour, 06:00–12:00 UTC
**Created**: 2026-04-23

## Context

You are running as a scheduled overnight observation agent for the WOS (Work Operating System) pipeline. Dan has authorized a full overnight run. Your role is to monitor pipeline health, recover stuck UoWs, and report a brief delta to Dan each hour.

## Instructions

### Step 1: Registry status breakdown

Run the following to get current UoW counts by status:

```python
import sys
from pathlib import Path
sys.path.insert(0, '/home/lobster/lobster/src')
from orchestration.registry import Registry
from collections import Counter

db_path = Path('/home/lobster/lobster-workspace/orchestration/registry.db')
r = Registry(db_path)
uows = r.list()
statuses = Counter(getattr(u, 'status', 'unknown') for u in uows)
for status, count in sorted(statuses.items()):
    print(f'{status}: {count}')
```

Record the current counts for each status.

### Step 2: Run startup-sweep to recover orphaned/stuck UoWs

```bash
cd /home/lobster/lobster && uv run scheduled-tasks/startup-sweep.py 2>&1 | tail -20
```

Note how many UoWs were recovered (if any).

### Step 3: Check executor-heartbeat has been firing

```bash
tail -30 /home/lobster/lobster-workspace/scheduled-jobs/logs/executor-heartbeat.log
```

Look for the most recent timestamp. If the last entry is more than 5 minutes old, the heartbeat may have stalled — note this in your report.

Check the steward-heartbeat similarly:

```bash
tail -20 /home/lobster/lobster-workspace/scheduled-jobs/logs/steward-heartbeat.log
```

### Step 4: Identify UoWs stuck in executing >10 minutes

```python
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/home/lobster/lobster/src')
from orchestration.registry import Registry

db_path = Path('/home/lobster/lobster-workspace/orchestration/registry.db')
r = Registry(db_path)

# Check for UoWs stuck in executing
# Note: no 'executing' status in this registry — check ready-for-steward with old started_at
# Check stale heartbeat UoWs instead
stale = r.get_stale_heartbeat_uows()
print(f'Stale heartbeat UoWs: {len(stale)}')
for u in stale:
    print(f'  {u.id}: started_at={u.started_at} heartbeat_at={u.heartbeat_at}')
```

If any UoWs are stale (heartbeat expired), call `complete_uow()` on them with a note explaining they were force-completed by the overnight observation agent after being stuck >10 minutes:

```python
for u in stale:
    r.complete_uow(u.id, output_ref='force-completed by wos-hourly-observation: heartbeat stall')
    print(f'Force-completed: {u.id}')
```

### Step 5: BOOTUP_CANDIDATE_GATE awareness

Note: All current ready-for-steward UoWs carry the `bootup-candidate` label, which is blocked by BOOTUP_CANDIDATE_GATE. This is expected system behavior — those UoWs will not be dispatched while the gate is active. Report the count of blocked UoWs in your status message to Dan.

If you find UoWs in ready-for-steward that do NOT carry `bootup-candidate`, that is a positive signal — report how many are unblocked.

### Step 6: Send brief status to Dan

Send a message to Dan (chat_id: 8075091586) with:
- Current time (UTC)
- Status breakdown: done/proposed/ready-for-steward/failed counts
- Delta since last hour (if you can infer from logs — e.g., "3 new done since last check")
- Heartbeat health: last executor-heartbeat timestamp
- Any stale UoWs found and force-completed
- Bootup-candidate gate status: N UoWs blocked

Keep it brief — Dan is on mobile.

Example format:
```
WOS hourly check — 08:00 UTC
done: 301 | proposed: 208 | ready: 27 | failed: 10
Heartbeat: firing every 3 min (healthy)
Bootup-candidate gate: 27 UoWs blocked (expected)
No stale UoWs found
```

Use `send_reply(chat_id=8075091586, text="...", source="telegram")`.

### Step 7: Write task output

Call `write_task_output` with:
- job_name: "wos-hourly-observation"
- output: Full status details (registry counts, heartbeat status, stale UoWs recovered, delta)
- status: "success" or "failed"

Then call `write_result` with:
- task_id: your task_id
- chat_id: 0
- sent_reply_to_user: True

## Output

When complete, call `write_task_output` with job_name "wos-hourly-observation" and full details, then `write_result` with sent_reply_to_user=True.
