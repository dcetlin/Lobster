# LOS Action Item Scanner

**Job**: los-action-scanner
**Schedule**: Hourly (`0 * * * *`)
**Created**: 2026-05-09

**Minimum viable output:** Extract action commitments from the past hour of user messages and write them to self_action_items.db. Acknowledge with a count of items found.
**Boundary:** Do not send unsolicited Telegram messages about individual items — extraction is silent. Only surface a count if items were found.

## Context

You are running as a scheduled LOS extraction task. Your job is to scan recent conversation history for action commitments Dan has made and persist them to the personal action items database.

The LOS (Life Operating System) database lives at:
`~/lobster-user-config/data/self_action_items.db`

## Instructions

### Step 1: Check enabled gate

```python
import json
from pathlib import Path

jobs_file = Path.home() / "lobster-workspace" / "scheduled-jobs" / "jobs.json"
data = json.loads(jobs_file.read_text())
if not data["jobs"].get("los-action-scanner", {}).get("enabled", True):
    print("Job disabled — exiting.")
    exit()
```

### Step 2: Scan recent conversation history

Call `get_conversation_history(limit=60, sender_type="user")` to retrieve the last hour of user messages.

Filter to messages from the past hour only (compare timestamp to `datetime.now(UTC) - timedelta(hours=1)`).

### Step 3: Extract action items

The scanner script emits candidate messages as JSON to stdout. Read that JSON, then for each candidate message:

1. Use your native intelligence to identify action commitments in the message text. Produce a JSON array of items in the format: `[{"text": "...", "priority": <1-10>}]`. If no action items are present, produce `[]`.
2. Call `parse_llm_response` on your JSON string to validate and normalise the items.
3. Call `extract_action_items` with the parsed items to persist them to the DB.

The extractor handles dedup automatically — no need to check manually.

```python
import sys
sys.path.insert(0, str(Path.home() / "lobster"))

from src.los.db import connect
from src.los.extractor import extract_action_items, parse_llm_response

conn = connect()
try:
    # candidates is the list from the scanner's JSON output:
    # {"candidates": [{"msg_id": "...", "text": "..."}]}
    for candidate in candidates:
        # Produce your own JSON extraction for this message text:
        raw_json = '<your extracted JSON array here>'
        items = parse_llm_response(raw_json)
        if not items:
            continue
        saved = extract_action_items(
            conn=conn,
            items=items,
            source="telegram",
            source_message_id=candidate.get("msg_id"),
        )
        if saved:
            print(f"Extracted: {[i.text for i in saved]}")
finally:
    conn.close()
```

### Step 4: Write result

Call `write_task_output(job_name="los-action-scanner", output=<summary>, status="success")`.

Summary format: "Scanned N messages, extracted M action items." (include item titles if M > 0)

Do NOT send a Telegram notification — this is a silent background job.
If extraction found 0 items, write_task_output with status="success" and output="No action items found."

## Output

When you complete your task, call `write_task_output` with:
- job_name: "los-action-scanner"
- output: Summary of what was found
- status: "success" or "failed"
