# Philosophy Discovery Scorer

**Job**: philosophy-discovery-scorer
**Schedule**: Daily at 23:30 local time (`30 23 * * *`)

## Context

You are a Lobster generalist subagent running the philosophy discovery scorer job.
Your role is to score the most recent philosophy-explore session as genuine discovery
vs. reconstruction, then log the result to the discovery JSONL file.

Read the scoring heuristic at:
`~/lobster-workspace/assessments/discovery-scoring.md`

## Instructions

### Step 0: Staleness gate — skip if file unchanged

Before doing any scoring work, check whether the most recent philosophy-explore
session file has changed since the last time this job ran. If it has not changed,
exit immediately without making any LLM calls.

**Find the most recent session file** (same logic as Step 1):

```bash
ls -t ~/lobster-workspace/philosophy-explore/*-philosophy-explore.md 2>/dev/null | head -1
```

Save the result as `LATEST_FILE`.

**Run the staleness check** using the reusable helper:

```bash
uv run -c "
import sys
sys.path.insert(0, '/home/lobster/lobster')
from src.utils.staleness_gate import file_changed
target = '$LATEST_FILE'
changed = file_changed(target, job_name='philosophy-discovery-scorer')
print('changed' if changed else 'unchanged')
"
```

- If the output is `unchanged`: call `write_task_output` with the message below and stop — do not proceed to Step 1.

  ```
  write_task_output(
      job_name="philosophy-discovery-scorer",
      output="Session file unchanged since last scan — skipped scoring (heat).",
      status="success"
  )
  ```

- If the output is `changed` (or if `LATEST_FILE` is empty): proceed to Step 1.

> **Pattern note:** This staleness gate is the canonical pattern for all
> schedule-driven file-scanning scorers. Any job that scans a file on a schedule
> should call `file_changed` at the top of its run, before any inference. See
> `src/utils/staleness_gate.py` for the helper's API and design rationale.

---

### Step 1: Find the most recent philosophy-explore session file

List files in `~/lobster-workspace/philosophy-explore/` sorted by modification time.
Identify the most recently written `*-philosophy-explore.md` file.

If no file exists, or if the most recent file was written more than 48 hours ago, write:

```
write_task_output(job_name="philosophy-discovery-scorer", output="No recent philosophy-explore session found (threshold: 48h). Nothing scored.", status="success")
```

Then stop.

### Step 2: Read the session file and apply the 5-signal heuristic

Read the full text of the session .md file. For each of the five signals defined in
`~/lobster-workspace/assessments/discovery-scoring.md`, determine if the signal is present (1)
or absent (0):

1. **Surprise Marker** — Did the session end with a conclusion the user explicitly marked
   as surprising or unexpected? Look for: "I hadn't thought of that", "that's surprising",
   "I didn't expect this", "huh", or similar meta-comments.

2. **Positional Reversal** — Did the session produce a reversal — a position held at the
   start that was abandoned or significantly modified by the end?

3. **Open Starting Thread** — Did the session begin with a genuine question (open) rather
   than a thesis or framework to apply (closed)? Score 1 if open, 0 if closed.

4. **Novel Frame or Term** — Did the session coin or arrive at a new term, analogy, or
   conceptual frame not present in the starting materials?

5. **Explicit Perspective Shift** — Did Dan (or the session transcript) signal "I hadn't
   thought of it that way" or equivalent — surprise about a framing, not just a conclusion?

If Dan's session file includes a frontmatter block `discovery_signals: [1, 0, 1, 0, 1]`,
use those values directly instead of inferring.

Compute: `discovery_score = (sum of present signals) / 5.0`

### Step 3: Collect condition metadata

From the session file and context:

- `session_start_time`: Extract from the file's date/time header (ISO8601, local timezone).
  The filename format `YYYY-MM-DD-HH00-philosophy-explore.md` encodes this.
- `time_of_day_bucket`: Derive from hour:
  - 06:00–11:59 → `morning`
  - 12:00–17:59 → `afternoon`
  - 18:00–21:59 → `evening`
  - 22:00–05:59 → `night`
- `initiation_source`: If the session was written by the scheduled `philosophy-explore-1`
  job (check if filename hour aligns with its schedule, every 4 hours), classify as
  `scheduled`. Otherwise classify as `user-initiated`.
- `starting_thread_type`: Read the opening of the "Today's Thread" section. Classify as:
  `question` (starts with a question), `thesis` (starts with a claim), `observation`
  (starts with noting something), or `continuation` (explicitly continues a prior thread).
- `session_length_turns`: Estimate from the number of `##` section headers and paragraphs.
  For a scheduled solo-subagent session, this is typically 1 (single agent pass). Use 1
  unless the transcript shows explicit multi-turn structure.
- `days_since_last_dan_interaction`: Use get_conversation_history to find the most recent
  message from a human (non-system) sender. Compute days between that message and today.
  If unavailable, use null.

### Step 4: Write the JSONL log entry

Append a single JSON line to `~/lobster-workspace/data/philosophy-discovery-log.jsonl`:

```python
import json, datetime

entry = {
    "session_start_time": "<ISO8601>",
    "time_of_day_bucket": "<morning|afternoon|evening|night>",
    "initiation_source": "<user-initiated|scheduled|lobster-suggested>",
    "starting_thread_type": "<question|thesis|observation|continuation>",
    "session_length_turns": <integer>,
    "days_since_last_dan_interaction": <integer or null>,
    "discovery_score": <float 0.0-1.0>,
    "signals": {
        "surprise_marker": <true|false>,
        "positional_reversal": <true|false>,
        "open_starting_thread": <true|false>,
        "novel_frame_or_term": <true|false>,
        "explicit_perspective_shift": <true|false>
    },
    "session_file": "<absolute path to source .md file>",
    "scored_at": "<ISO8601 now>"
}
```

Use `uv run` to execute a Python snippet that appends this entry. Example:

```bash
uv run -c "
import json
entry = { ... }
with open('/home/lobster/lobster-workspace/data/philosophy-discovery-log.jsonl', 'a') as f:
    f.write(json.dumps(entry) + '\n')
print('Logged:', json.dumps(entry, indent=2))
"
```

### Step 5: Record the scan and write task output

**Record the scan** so the next run sees the updated baseline:

```bash
uv run -c "
import sys
sys.path.insert(0, '/home/lobster/lobster')
from src.utils.staleness_gate import record_scan
record_scan('$LATEST_FILE', job_name='philosophy-discovery-scorer')
print('Staleness record updated.')
"
```

Then call:

```
write_task_output(
    job_name="philosophy-discovery-scorer",
    output="Scored session: <session_file_basename>. discovery_score=<score> (signals: <list of present signal names>). Entry appended to philosophy-discovery-log.jsonl.",
    status="success"
)
```

If any step fails, write status="failed" with a description of what went wrong.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "philosophy-discovery-scorer"
- output: Your results/summary
- status: "success" or "failed"
