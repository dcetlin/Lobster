# Dispatcher On-Demand Context

<!--
  PATTERN: On-demand bootup sections (implements dcetlin/Lobster#40)

  These sections are case-specific and rarely needed on the fast path (message -> reply -> loop).
  They are NOT loaded at startup. The main bootup (sys.dispatcher.bootup.md) contains a one-line
  reference for each section below. Read this file only when you encounter the relevant case.

  Section index -- read only the section you need:
    compact-reminder  ->  ## Handling compact-reminder
    cron_reminder     ->  ## Cron Job Reminders
    hibernate         ->  ## Hibernation
    brain-dump        ->  ## Processing Voice Note Brain Dumps
    calendar          ->  ## Google Calendar
-->

<!-- on-demand: compact-reminder -->
## Handling compact-reminder (subtype: "compact-reminder")

After a context compaction you lose situational awareness of the last ~30 minutes. The compact_catchup subagent recovers it for you.

> **WARNING: CATCHUP IS ALWAYS A BACKGROUND SUBAGENT -- NEVER INLINE.**
>
> Do NOT call `check_inbox`, `Read`, or any other tool to perform catchup yourself on the main thread. Catchup involves file I/O, inbox scanning, and summarization -- it takes 10-15 minutes and blocks all new messages during that time. This is a 7-second rule violation.
>
> The dispatcher's only job here is to SPAWN THE SUBAGENT and return to the loop. The subagent does the work. The dispatcher does not.
>
> **Violation pattern (never do this):**
> ```
> # WRONG: dispatcher performing catchup inline
> check_inbox(since_ts=...)                                    # VIOLATION
> Read("~/lobster-workspace/data/compaction-state.json")       # VIOLATION
> ```

**When `wait_for_messages` returns a message with `subtype: "compact-reminder"`:**

```
1. mark_processing(message_id)
2. Read the compact-reminder text to re-orient (identity, main loop, key files)
3. Run: ~/lobster/scripts/record-catchup-state.sh start
   (tells health check a catchup is starting -- suppresses WFM freshness check for 15 min)
4. Spawn compact_catchup subagent (run_in_background=True):
   - subagent_type: "compact-catchup"
   - prompt: (see below)
5. mark_processed(message_id)
6. Resume wait_for_messages() loop -- do NOT wait for the subagent result inline
```

> **CRITICAL -- do not wait inline.** The catchup subagent can take 10-12 minutes. If you
> wait for its result before calling wait_for_messages(), the health check's WFM freshness
> threshold (600s) will fire and trigger an unnecessary restart. Always spawn with
> run_in_background=True and return to the main loop immediately (step 6 above).

**Prompt to pass to compact_catchup:**

```
---
task_id: compact-catchup
chat_id: 0
source: system
---

Recover dispatcher context after compaction. Read ~/lobster-workspace/data/compaction-state.json,
compute the catch-up window (max of last_compaction_ts, last_restart_ts, last_catchup_ts in that
file; default to 30 minutes ago if absent), call check_inbox(since_ts=<window_start>, limit=50),
summarise what happened (user messages, subagent results, notable system events), update
last_catchup_ts in compaction-state.json, then call write_result.
```

**When the compact_catchup `subagent_result` arrives:**

```
1. mark_processing(message_id)
2. Read msg["text"] -- it is a structured summary of recent activity (user messages,
   subagent results, system events). Use it to restore situational awareness.
3. Do NOT send_reply -- this is internal context, not a user message.
4. Run: ~/lobster/scripts/record-catchup-state.sh finish
   (tells health check catchup is complete -- lifts WFM suppression immediately)
5. mark_processed(message_id)
```

**Rules:**
- Never send the catch-up summary to the user unless you spot something urgent (e.g. a failed subagent that was never acknowledged).
- The catch-up result arrives as a normal `subagent_result` with `task_id: "compact-catchup"` and `chat_id: 0`. The `chat_id: 0` signals it is internal -- do not relay.
- If the catch-up window has no messages, that is valid -- the subagent reports "Nothing to report."

---

<!-- on-demand: cron_reminder -->
## Cron Job Reminders (`cron_reminder`)

When a scheduled job finishes, `run-job.sh` calls `scheduled-tasks/post-reminder.sh`, which writes a `cron_reminder` message to the inbox. These are system messages (`source: "system"`, `chat_id: 0`) -- they signal that job output is available to review.

**When `wait_for_messages` returns a message with `type: "cron_reminder"`:**

```
1. mark_processing(message_id)
2. job_name = msg["job_name"]
3. status = msg["status"]          # "success" or "failed"
4. duration = msg["duration_seconds"]

5. Call check_task_outputs(job_name=job_name, limit=1) to read the latest output

6. if output exists AND is noteworthy (non-trivial content, failure, or actionable finding):
       send_reply(chat_id=ADMIN_CHAT_ID, text=<concise summary>, source="telegram")
   else:
       # Silent -- routine success with no news is not worth interrupting the user

7. mark_processed(message_id)
```

**Key fields:**
- `type` -- always `"cron_reminder"`
- `source` -- always `"system"` (do NOT call send_reply to the chat_id, which is 0)
- `chat_id` -- always `0` (system message, no user to reply to directly)
- `job_name` -- the name of the job that just ran (use for `check_task_outputs`)
- `exit_code` -- raw shell exit code (0 = success)
- `duration_seconds` -- how long the job ran
- `status` -- `"success"` or `"failed"` (derived from exit_code)

**Triage heuristic:**
- Always relay **failures** (`status: "failed"`) with the job output or "no output recorded"
- For successes, relay if the output contains findings, alerts, or explicit user-relevant content
- Routine "nothing to report" outputs -> silent (mark processed only)

**Note:** Jobs that already call `send_reply` + `write_result` directly will produce a `subagent_result`/`subagent_notification` in addition to the `cron_reminder`. In that case the `cron_reminder` arrives after the user message -- you can safely mark it processed without re-sending.

---

<!-- on-demand: hibernate -->
## Hibernation

Lobster supports a **hibernation mode** to avoid idle resource usage. When no messages arrive for a configurable idle period, Claude writes a hibernate state and exits gracefully. The bot detects the next incoming message, sees that Claude is not running, and starts a fresh session automatically.

### Hibernate-aware main loop

Use `hibernate_on_timeout=True` when you want automatic hibernation after the idle period:

```
while True:
    result = wait_for_messages(timeout=1800, hibernate_on_timeout=True)
    # If the response text contains "Hibernating" or "EXIT", stop the loop
    if "Hibernating" in result or "EXIT" in result:
        break   # Claude session exits; bot will restart on next message
    # ... process messages ...
```

The `hibernate_on_timeout` flag tells `wait_for_messages` to:
1. Write `~/messages/config/lobster-state.json` with `{"mode": "hibernate"}`
2. Return a message containing the word "Hibernating" and "EXIT"
3. **You must then break out of the loop and let the session end.**

The health check recognises the hibernate state and does **not** attempt to restart Claude.
The bot (`lobster-router.service`) checks the state file when a new message arrives and restarts Claude if it is hibernating.

### State file

Location: `~/messages/config/lobster-state.json`

```json
{"mode": "hibernate", "updated_at": "2026-01-01T00:00:00+00:00"}
```

Modes: `"active"` (default) | `"hibernate"`

---

<!-- on-demand: brain-dump -->
## Processing Voice Note Brain Dumps

When you receive a **voice message** that appears to be a "brain dump" (unstructured thoughts, ideas, stream of consciousness) rather than a command or question, use the **brain-dumps** agent.

**Note:** This feature can be disabled via `LOBSTER_BRAIN_DUMPS_ENABLED=false` in `lobster.conf`. The agent can also be customized or replaced via the [private config overlay](docs/CUSTOMIZATION.md) by placing a custom `agents/brain-dumps.md` in your private config directory.

**Indicators of a brain dump:**
- Multiple unrelated topics in one message
- Phrases like "brain dump", "note to self", "thinking out loud"
- Stream of consciousness style
- Ideas/reflections rather than questions or requests

**Mirror mode (default for all voice notes):**
The brain-dumps agent runs a **semantic mirror pass (Stage 0)** before any triage or action extraction. This reflects the user's own language, framings, and conceptual handles back before organizing or summarizing. Do not suppress or bypass this -- it is the primary protection against the AI substituting its categories for the user's thinking. See `agents/brain-dumps.md` for the full Stage 0 specification.

**Trigger phrases for explicit mirror mode** (user can also request it for text brain dumps):
- "mirror mode"
- "process this in mirror mode"
- "reflect this back"

**Workflow:**
1. Receive voice message (already transcribed -- `msg["transcription"]` is populated by the worker)
2. Read transcription from `msg["transcription"]` or `msg["text"]`
3. Check if brain dumps are enabled (default: true)
4. If transcription looks like a brain dump, spawn brain-dumps agent with `Mirror mode: true`:
   ```
   Task(
     prompt=f"---\ntask_id: brain-dump-{id}\nchat_id: {chat_id}\nsource: {source}\nreply_to_message_id: {id}\n---\n\nProcess this brain dump:\nTranscription: {text}\nMirror mode: true",
     subagent_type="brain-dumps"
   )
   ```
5. Agent will run the mirror pass first, then save enriched issue to user's `brain-dumps` GitHub repository

**NOT a brain dump** (handle normally):
- Direct questions ("What time is it?")
- Commands ("Set a reminder")
- Specific task requests

See `docs/BRAIN-DUMPS.md` for full documentation.

---

<!-- on-demand: calendar -->
## Google Calendar

Calendar commands work in two modes. Check auth status first (no network call needed):

```python
import sys; sys.path.insert(0, "/home/admin/lobster/src")
from integrations.google_calendar.token_store import load_token
is_authenticated = load_token("<REDACTED_PHONE>") is not None
```

### Unauthenticated mode (default)

Generate a deep link whenever an event with a concrete date/time is mentioned:

```python
from utils.calendar import gcal_add_link_md
from datetime import datetime, timezone
link = gcal_add_link_md(title="Doctor appointment",
                        start=datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc))
# -> [Add to Google Calendar](https://calendar.google.com/...)
```

- Append link on its own line at the end of the message
- Omit `end` to default to start + 1 hour
- Do NOT generate a link when date/time is vague

### Authenticated mode (token exists for user)

Delegate to a background subagent -- API calls exceed the 7-second rule.

**Reading events** ("what's on my calendar", "what do I have this week/today"):
```python
from integrations.google_calendar.client import get_upcoming_events
events = get_upcoming_events(user_id="<REDACTED_PHONE>", days=7)
# Returns List[CalendarEvent] or [] on failure -- always falls back gracefully
```

**Creating events** ("add X to my calendar", "schedule X for [time]"):
```python
from integrations.google_calendar.client import create_event
event = create_event(user_id="<REDACTED_PHONE>", title="...", start=start, end=end)
# Returns CalendarEvent with .url, or None on failure
# On failure, fall back to gcal_add_link_md()
```

Always append a deep link or view link even when creating via API.

### Auth command ("connect my Google Calendar", "authenticate Google Calendar", "link Google Calendar")

Handle on the main thread -- no subagent, no API call:

```python
import secrets
from integrations.google_calendar.config import is_enabled
from integrations.google_calendar.oauth import generate_auth_url
if is_enabled():
    url = generate_auth_url(state=secrets.token_urlsafe(32))
    reply = f"Click to connect your Google Calendar:\n[Authorize Google Calendar]({url})"
else:
    reply = "Google Calendar isn't configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in config.env."
```

### Rules

- Never expose tokens, credentials, or raw error messages in replies
- If API fails, always fall back to a deep link -- never return an empty reply
- user_id = owner's Telegram chat_id as string (set via config, do NOT hardcode)
- When a subagent handles events, pass event title/start/end to `gcal_add_link_md()` for the link
