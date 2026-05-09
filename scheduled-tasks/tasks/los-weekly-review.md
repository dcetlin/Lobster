# LOS Weekly Review

**Job**: los-weekly-review
**Schedule**: Sundays at 08:00 (`0 8 * * 0`)
**Created**: 2026-05-09

**Minimum viable output:** A Telegram message listing dismissed items from the past 7 days so Dan can see what he waved off.
**Boundary:** Read-only. Do not modify status of any item. No buttons (this is a review, not a management interface).

## Context

You are running as a weekly LOS review task. Surface the dismissed action items from the past week so Dan can see what he chose not to act on.

## Instructions

### Step 1: Check enabled gate

```python
import json
from pathlib import Path

jobs_file = Path.home() / "lobster-workspace" / "scheduled-jobs" / "jobs.json"
data = json.loads(jobs_file.read_text())
if not data["jobs"].get("los-weekly-review", {}).get("enabled", True):
    print("Job disabled — exiting.")
    exit()
```

### Step 2: Query dismissed items from the past 7 days

```python
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path.home() / "lobster"))

from src.los.db import connect, get_dismissed_items_since

since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
conn = connect()
try:
    dismissed = get_dismissed_items_since(conn, since_iso=since)
finally:
    conn.close()
```

### Step 3: Format and send

If `dismissed` is empty:
- Send: "Weekly review: No dismissed items from the past 7 days."

If dismissed items exist:
- Send: "Weekly review: {N} items dismissed this week:\n\n{formatted_list}"

Format each dismissed item:
```
  - {item.text} (dismissed {dismissed_at_local_date})
```

### Step 4: Write result

Call `write_result(task_id=<your task_id>, chat_id=8075091586, text=<summary>, sent_reply_to_user=True, status="success")`.

And call `write_task_output(job_name="los-weekly-review", output=<summary>, status="success")`.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "los-weekly-review"
- output: Summary of dismissed items count
- status: "success" or "failed"
