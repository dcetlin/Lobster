# WOS Pipeline State Query

**Triggered by**: `/wos` command from Dan
**Type**: On-demand subagent (not a scheduled job)

**Minimum viable output:** A formatted WOS pipeline summary sent to Dan via Telegram.
**Boundary:** Read-only. Do not modify wos.db or any other files. Do not send multiple messages.

## Context

Dan sent the `/wos` command. Your job is to query the WOS database for live UoW status
counts, read the Bisque dashboard link, format a summary, and send it.

## Instructions

### Step 1: Query WOS pipeline state

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "lobster"))

from src.orchestration.registry import Registry

registry = Registry()
status_counts = registry.get_status_counts()
```

### Step 2: Read the Bisque dashboard link

```python
dashboard_path = Path.home() / "lobster-workspace/workstreams/wos/dashboard.md"
bisque_url = None
if dashboard_path.exists():
    for line in dashboard_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("- **URL:**"):
            # Extract URL from "- **URL:** http://..."
            bisque_url = line.split("- **URL:**")[-1].strip()
            break
```

### Step 3: Format the message

```python
# Build pipeline summary lines, ordered by relevance
STATUS_ORDER = [
    "active",
    "ready-for-steward",
    "needs-human-review",
    "blocked",
    "pending",
    "proposed",
    "executing",
    "paused",
    "completed",
    "done",
    "expired",
    "cancelled",
]

active_count = status_counts.get("active", 0)

# Build ordered status lines (non-zero statuses first in canonical order, then extras)
ordered = [(s, status_counts[s]) for s in STATUS_ORDER if s in status_counts]
extras = [(s, c) for s, c in sorted(status_counts.items()) if s not in STATUS_ORDER]
all_statuses = ordered + extras

total = sum(status_counts.values())
pipeline_lines = "\n".join(f"  {s}: {c}" for s, c in all_statuses if c > 0)

msg_parts = [
    f"WOS pipeline — {active_count} active UoW{'s' if active_count != 1 else ''}",
    "",
    "Pipeline breakdown:",
    pipeline_lines or "  (empty)",
    f"  ─────",
    f"  total: {total}",
]

if bisque_url:
    msg_parts += ["", f"Dashboard: {bisque_url}"]

msg = "\n".join(msg_parts)
```

### Step 4: Send and complete

```python
send_reply(chat_id=chat_id, text=msg, source=source, task_id=task_id)

write_result(
    task_id=task_id,
    chat_id=chat_id,
    text=msg,
    sent_reply_to_user=True,
    status="success",
    source=source,
)
```
