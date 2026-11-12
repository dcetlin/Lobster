# CC Quota Query

**Triggered by**: `/quota` command from Dan
**Type**: On-demand subagent (not a scheduled job)

**Minimum viable output:** A formatted CC usage message sent to Dan via Telegram.
**Boundary:** Read-only. Do not modify any files or send multiple messages.

## Context

Dan sent the `/quota` command. Your job is to read the CC budget state file and
send a single formatted usage message.

## Instructions

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "lobster"))

from src.orchestration.dispatcher_handlers import read_quota_state, format_quota_message

state = read_quota_state()
msg = format_quota_message(state)
```

Send a single Telegram message with the formatted quota text:

```python
send_reply(chat_id=chat_id, text=msg, source=source, task_id=task_id)
```

Then call write_result:

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
