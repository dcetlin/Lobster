# System Status Query

**Triggered by**: `/status` command from Dan
**Type**: On-demand subagent (not a scheduled job)

**Minimum viable output:** A formatted system status snapshot sent to Dan via Telegram.
**Boundary:** Read-only. Do not modify any files. Do not send multiple messages.

## Context

Dan sent the `/status` command. Your job is to collect three data sources and
send a single formatted status snapshot.

## Instructions

### Step 1: Gather data

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "lobster"))

from src.orchestration.dispatcher_handlers import read_quota_state, read_wos_config
from src.orchestration.registry import Registry
from src.agents.session_store import get_active_sessions

# CC quota state
quota_state = read_quota_state()

# WOS config (execution_enabled, etc.)
wos_config = read_wos_config()

# WOS queue depth by status
registry = Registry()
status_counts = registry.get_status_counts()

# Active background agents
active_sessions = get_active_sessions()
```

### Step 2: Format and send

```python
from src.orchestration.dispatcher_handlers import format_status_message

msg = format_status_message(
    active_sessions=active_sessions,
    wos_config=wos_config,
    status_counts=status_counts,
    quota_state=quota_state,
)

send_reply(chat_id=chat_id, text=msg, source=source, task_id=task_id)
```

### Step 3: Write result

```python
write_result(
    task_id=task_id,
    chat_id=chat_id,
    text=msg,
    sent_reply_to_user=True,
    status="success",
    source=source,
)
```
