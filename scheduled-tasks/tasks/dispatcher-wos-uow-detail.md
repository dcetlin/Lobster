# WOS UoW Detail Query

**Triggered by**: `/wos uow <id>` command from Dan
**Type**: On-demand subagent (not a scheduled job)

**Minimum viable output:** A formatted UoW detail message sent to Dan via Telegram.
**Boundary:** Read-only. Do not modify wos.db or any other files. Do not send multiple messages.

## Context

Dan sent `/wos uow <uow_id>`. Your job is to look up that UoW in the registry, format a
detail summary, and send it. The `uow_id` variable is injected into this prompt by the
dispatcher — it will be the raw ID string Dan typed (e.g. `uow_20260501_abc123` or a
short suffix like `abc123`).

## Instructions

### Step 1: Look up the UoW

```python
import sys, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path.home() / "lobster"))

from src.orchestration.registry import Registry
from src.orchestration.paths import REGISTRY_DB

registry = Registry()

# Try exact match first
uow = registry.get(uow_id)

# If not found, try suffix match (user may have typed just the trailing hex, e.g. "abc123")
if uow is None:
    conn = sqlite3.connect(str(REGISTRY_DB), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id FROM uow_registry WHERE id LIKE ? ORDER BY created_at DESC",
            (f"%{uow_id}",),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) == 1:
        uow = registry.get(rows[0]["id"])
    elif len(rows) > 1:
        ids = ", ".join(f"`{r['id']}`" for r in rows[:5])
        reply = f"Ambiguous ID — {len(rows)} UoWs end with `{uow_id}`: {ids}"
        send_reply(chat_id=chat_id, text=reply, source=source, task_id=task_id)
        write_result(task_id=task_id, chat_id=chat_id, text=reply, sent_reply_to_user=True, status="success", source=source)
        raise SystemExit(0)
```

### Step 2: Handle not-found

```python
if uow is None:
    reply = f"UoW `{uow_id}` not found in registry."
    send_reply(chat_id=chat_id, text=reply, source=source, task_id=task_id)
    write_result(task_id=task_id, chat_id=chat_id, text=reply, sent_reply_to_user=True, status="success", source=source)
    raise SystemExit(0)
```

### Step 3: Fetch extra fields not in the UoW dataclass

```python
# outcome_category and gate_fired are DB columns not mapped to the UoW dataclass.
# Fetch them via direct SQL.
conn2 = sqlite3.connect(str(REGISTRY_DB), timeout=10.0)
conn2.execute("PRAGMA journal_mode=WAL")
conn2.execute("PRAGMA busy_timeout=5000")
conn2.row_factory = sqlite3.Row
try:
    extra_row = conn2.execute(
        "SELECT outcome_category, gate_fired, completed_at FROM uow_registry WHERE id = ?",
        (uow.id,),
    ).fetchone()
finally:
    conn2.close()

outcome_category = extra_row["outcome_category"] if extra_row else None
gate_fired = extra_row["gate_fired"] if extra_row else None
completed_at = extra_row["completed_at"] if extra_row else None
```

### Step 4: Format the detail message

```python
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def fmt_ts(ts):
    """Format ISO timestamp to concise local-time display using LOBSTER_USER_TZ."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        tz_name = os.environ.get("LOBSTER_USER_TZ", "America/New_York")
        local_dt = dt.astimezone(ZoneInfo(tz_name))
        return local_dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return ts[:16]

def fmt_duration(start, end):
    """Compute and format elapsed time between two ISO timestamps."""
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        secs = int((t1 - t0).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        h, rem = divmod(secs, 3600)
        return f"{h}h {rem // 60}m"
    except Exception:
        return None

# The completed_at from the extra row; fall back to closed_at if present
end_ts = completed_at or uow.closed_at

# Status line: append outcome category if present
status_str = str(uow.status)
if outcome_category:
    status_str += f" / {outcome_category}"

# Elapsed time
elapsed = fmt_duration(uow.started_at, end_ts)

# Cycle counts
cycles_parts = [f"steward: {uow.steward_cycles}"]
if uow.lifetime_cycles and uow.lifetime_cycles != uow.steward_cycles:
    cycles_parts.append(f"lifetime: {uow.lifetime_cycles}")
if uow.execution_attempts:
    cycles_parts.append(f"exec attempts: {uow.execution_attempts}")
if uow.retry_count:
    cycles_parts.append(f"retries: {uow.retry_count}")
cycles_str = ", ".join(cycles_parts)

# Prescription confidence
conf_str = ""
if uow.prescription_confidence is not None:
    pct = round(uow.prescription_confidence * 100)
    conf_str = f" ({pct}% confidence)"

lines = [
    f"UoW: `{uow.id}`",
    f"Status: {status_str}",
    f"Summary: {uow.summary or '(none)'}",
    "",
    f"Created:   {fmt_ts(uow.created_at)}",
    f"Started:   {fmt_ts(uow.started_at)}",
    f"Completed: {fmt_ts(end_ts)}" + (f" ({elapsed})" if elapsed else ""),
    "",
    f"Cycles: {cycles_str}{conf_str}",
    f"Register: {uow.register or '—'}  Source: {uow.source or '—'}",
]

# Issue link
if uow.issue_url and uow.source_issue_number:
    lines.append(f"Issue: {uow.issue_url}")
elif uow.source_issue_number:
    lines.append(f"Issue: #{uow.source_issue_number}")

# Gate fired
if gate_fired and gate_fired not in ("none", "null", ""):
    lines.append(f"Gate fired: {gate_fired}")

# Close reason (if present and terminal)
if uow.close_reason:
    reason = uow.close_reason.strip()
    if len(reason) > 200:
        reason = reason[:197] + "..."
    lines += ["", f"Close reason: {reason}"]

msg = "\n".join(lines)
```

### Step 5: Send and complete

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
