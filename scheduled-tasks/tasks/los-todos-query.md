# LOS Todos Query

**Triggered by**: `/todos` command from Dan
**Type**: On-demand subagent (not a scheduled job)

**Minimum viable output:** A formatted list of open action items with Telegram inline buttons for Done/Snooze/Dismiss.
**Boundary:** Do not modify the DB. Read-only query.

## Context

Dan sent the `/todos` command. Your job is to query `self_action_items.db` and return the current list of open action items with interactive buttons.

## Instructions

### Step 1: Query open items

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "lobster"))

from src.los.db import connect, get_open_items

conn = connect()
try:
    items = get_open_items(conn, limit=10)
finally:
    conn.close()
```

### Step 2: Handle empty list

If `items` is empty:
- Send a single Telegram message: "No open action items. Great job!"
- Call `write_result` with status="success"
- Done.

### Step 3: Format and send each item with buttons

For each item, send a separate Telegram message with inline buttons.

Priority label mapping:
- 1-3 → "urgent"
- 4-6 → "medium"
- 7-10 → "low"

Message format per item:
```
[priority_label] {item.text}
(source: {item.source}, extracted {time_ago})
```

Buttons (pass as `buttons` parameter to `send_reply`):
```python
buttons = [
    [
        {"text": "Done", "callback_data": f"todo-done-{item.id}"},
        {"text": "Dismiss", "callback_data": f"todo-dismiss-{item.id}"},
    ],
    [
        {"text": "Snooze 3d", "callback_data": f"todo-snooze-{item.id}-{snooze_3d_date}"},
        {"text": "Snooze 1w", "callback_data": f"todo-snooze-{item.id}-{snooze_1w_date}"},
    ],
]
```

Where `snooze_3d_date` and `snooze_1w_date` are YYYY-MM-DD strings relative to today.

Send a summary first: "You have N open action items:"

### Step 4: Write result

Call `write_result(task_id=<your task_id>, chat_id=<chat_id>, text="Delivered N todos", sent_reply_to_user=True, status="success")`.

## Output

Call `write_result` with:
- task_id: your assigned task_id
- chat_id: Dan's chat_id
- text: "Delivered N open todos to Telegram"
- sent_reply_to_user: True
- status: "success"
