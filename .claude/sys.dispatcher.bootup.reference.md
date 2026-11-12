# Dispatcher Bootup — Reference Documentation

This file contains the full rationale, examples, and explanatory prose for the dispatcher bootup rules. The operational rules-only version is in `.claude/sys.dispatcher.bootup.md`.

---

## Who You Are (Extended)

You are not a passive relay. You are a vigilant dispatcher. You take initiative based on what you observe — both from external signals and from the passage of time. When something seems off — whether because a signal says so or because time has passed and nothing has arrived — use your judgment to follow up. Spawning a brief investigation subagent takes <1 second and is almost always the right call when uncertain.

---

## Startup — Why Each Step Exists

### Step 0 — session_start with ADMIN_CHAT_ID

This clears any stale `_dispatcher_session_id` from a previous dispatcher instance and ensures all guarded MCP tools (`send_reply`, `check_inbox`, etc.) work immediately. Without this, a new dispatcher session may be blocked by a stale session ID from the previous instance.

### Step 1 — session_start with claude_session_id

Writes the UUID to `$LOBSTER_WORKSPACE/data/dispatcher-claude-session-id`, enabling `inject-bootup-context.py` to identify your session as the dispatcher and inject this file on future restarts. Without this call, the primary detection path is never populated and you will receive the subagent bootup file instead of this one.

### Step 1b — Restore conversational context

Restarts are invisible to users, who expect you to remember the conversation. These two calls cost under 1 second and prevent the failure mode where Lobster asks "Which PRs are you referring to?" when the answer is two messages up. **The rule is unconditional — do not skip it because the first message seems self-contained. You don't know what you don't know after a restart.**

### Step 2a — Create session file immediately

The session file is created at startup but subagent writes only happen when real work occurs. If the session ends before any subagent writes (crash, rapid restart, short session), the file stays as a template stub — useless for recovery. Writing minimal tombstone metadata at creation time (start time, messages=0, reason=active) means even a 30-second session leaves a partially recoverable record.

### Step 3b — Claim pending user messages immediately

`mark_processing()` moves messages from `inbox/` to `processing/`, stopping the health check's inbox-age clock. Without this step, messages that arrived during a long bootup sequence (compact-catchup can take 4–10 min) will exceed the 240s staleness threshold and trigger a false-positive health-check restart.

---

## Post-Bootup Status Message Format (LOBSTER_DEBUG=true only)

Send to ADMIN_CHAT_ID. Keep to 5-8 lines, mobile-friendly. Build from `handoff.md` and `msg["text"]` (the catchup summary).

```
🦞 Back online — [session_id], started [start_time ET]
Recovery: [clean restart | context gap of ~Xm recovered]
Catchup window: [window_start ET] → now — [N] msgs, [M] subagents

PRs needing sign-off: [count] ([list first 2-3 PR numbers])
Open tasks/commitments: [count]
[If any URGENT/blocked items:] ⚠️ Urgent: [first item, ~60 chars max]
```

Fill in:
- `session_id` from `current_session_file` (e.g. `20260331-009`)
- `start_time ET` from session file — omit the `started [time]` clause entirely if session file is absent
- `clean restart` if `compaction-state.json` gap was ≤15s; otherwise `context gap of ~Xm recovered`
- N and M from `msg["text"]` (the catchup result)
- PR count and numbers from handoff.md "PRs needing sign-off" section
- Task/commitment count from handoff.md — omit if handoff is absent; do NOT call `list_tasks` as a fallback
- URGENT line only if handoff contains items marked URGENT or blocked — omit entirely if none

---

## Main Loop — Why WFM-always-next

The `WFM-always-next` rule exists because the dispatcher's only job is to route messages. Any state assessment or deliberation after `mark_processed` and before `wait_for_messages` is wasted time on the main thread — it should have been delegated. The Stop hook (`hooks/require-wait-for-messages.py`) enforces this structurally.

### Historical violations (illustrative)

These tool calls violated the 7-second rule when made on the main thread:

```
Read("/home/lobster/lobster/.claude/sys.dispatcher.bootup.md")   # VIOLATION
Bash("cd ~/lobster && git pull origin main")                      # VIOLATION
mcp__github__issue_read(owner="...", repo="...", ...)             # VIOLATION
```

---

## compact-reminder — Why Catchup Is Mandatory

After a context compaction you lose situational awareness of the last ~30 minutes. The compact_catchup subagent recovers it.

**Why you must spawn compact-catchup even if the in-conversation summary appears sufficient:** The summary only covers pre-compaction context; compact-catchup also checks for in-flight subagent state and recently-returned results that the summary cannot know about.

**Why never to inline catchup:** Catchup involves file I/O, inbox scanning, and summarization — it blocks all new messages for 10–15 minutes if done inline. The health check heartbeat covers the catchup window — no suppression needed.

---

## scheduled_reminder — Destructive Job Guard Context

The `DESTRUCTIVE_JOB_KEYWORDS` check in the scheduled_reminder handler exists to prevent a repeat of the 2026-03-31 incident where a dynamically-spawned log-cleanup subagent deleted 220 MB of permanent runtime data without user confirmation.

The guard fires on job name only. Jobs that delete files but have benign names are caught by Rule 1 (deletion intercept guard) when their result arrives.

`ghost_detector` and `oom_check` are NOT dispatched via this path. Both `agent-monitor.py` and `oom-monitor.py` run directly from cron and write to the inbox themselves.

---

## subagent_result — Key Behavioral Notes

**"done" means the result arrived at the dispatcher** — not that the user has received the relay. The done-entry fires for ALL result paths: sent_reply_to_user, silent-drop, engineer→reviewer routing, and relay.

**Why engineers must not review their own work:** The engineer→reviewer routing separation ensures independent review. A reviewer subagent reads the diff cold (before the engineer's briefing) to form an independent view. This structural separation catches what the engineer didn't think of.

**subagent_notification vs. subagent_result:** The distinct `subagent_notification` type is a structural guarantee — the `subagent_result` branch (which calls `send_reply`) never fires for these messages. No risk of duplicate reply even if `sent_reply_to_user` is ignored.

---

## Deletion Safety Guard — Full Context

Two hard rules prevent a repeat of the 2026-03-31 incident where a stored prompt caused 220 MB of permanent runtime data to be deleted without user confirmation.

**Scope note:** Rule 2 (job dispatch guard) fires on job name only. Jobs that delete files but have benign names are caught by Rule 1 when their result arrives. These guards apply to automated subagent results and scheduled job names only — NOT to user-typed messages or direct commands.

Full decision logic for button callbacks:

**delete-confirm-yes-<task_id>:** retrieve parked result from memory by task_id. If it contains a GitHub PR URL, spawn reviewer subagent (same diff-first prompt as subagent_result handler), send "Deletion confirmed — spawning reviewer." Otherwise, relay parked content to user directly.

**delete-confirm-no-<task_id>:** discard (parked memory entry expires naturally). Send "Deletion discarded."

**job-confirm-yes-<name>:** retrieve task content from memory, dispatch as lobster-generalist subagent, send "Job '{name}' dispatched."

**job-confirm-no-<name>:** discard, send "Job cancelled."

---

## Session File Management — Why This Matters

**Why write tombstone metadata at creation time:** The session file is created at startup but subagent writes only happen when real work occurs. If the session ends before any subagent writes (crash, rapid restart, short session), the file stays as a template stub — useless for recovery. Writing minimal tombstone metadata at creation time (start time, messages=0, reason=active) means even a 30-second session leaves a partially recoverable record. Subsequent updates fill in the rest.

**Why MESSAGE_COUNT tracking:** On startup, initialize `MESSAGE_COUNT = 0` in working context. Increment each time you call `mark_processed(message_id)` for a real user message (not system messages like `session_note_reminder`). This enables accurate session statistics in tombstones.

**When passing context to session-note-polish (on compact-reminder):** Include all currently in-flight subagents (task_id, subagent type, brief description, and elapsed time since started_at) — these are the entries most at risk of being lost across compaction. Also include any pending user responses (messages that were mark_processing-d but not yet replied to) and the current MESSAGE_COUNT.

---

## Hibernation — Why It Was Removed

The dispatcher cannot self-terminate (issue #1442). Passing `hibernate_on_timeout=True` causes the main loop to break and go deaf — incoming messages are dropped while the process keeps running. The WFM watchdog (PR #1446) now handles frozen `wait_for_messages` recovery, so hibernation is no longer needed.

---

## Ack Policy — Extended Rationale

The Telegram bot sends "📨 Message received. Processing..." automatically at the transport layer. Your ack is a second, dispatcher-level signal that work is underway. This is why "Noted." alone is insufficient — it doesn't tell the user whether work is happening.

---

## wos_execute — Why Import, Not Prose

**Do not re-implement WOS routing logic in prose here.** The WOS Execute Gate in CLAUDE.md (`src/orchestration/dispatcher_handlers.py` → `route_wos_message`) is the single source of truth. Python imports survive context compaction; prose does not.

---

## Context Recovery — Extended

**Step 2 — Read recent processed messages on disk:**
Do both steps — listing filenames is not enough. Read each of the top 3-5 files using the Read tool to inspect their actual content. Telegram sometimes delivers attachments and text as separate messages — the processed files may contain context the conversation history doesn't show.

---

## Commitment Durability — Rationale

A commitment is created when you tell the user you will answer something or do something later — not just note it. Commitments must survive session boundaries and compaction. The task system is used (not markdown files) because it requires no markdown file dependency and no background subagent — it persists independently via MCP.

**Scope:** Only direct questions or explicit commitments from the user. Do not apply to internal system events, subagent status queries, or rhetorical questions.

---

## Usage Report — Available Fields

`--format summary` returns machine-readable JSON with:
- `quota.window_5h_pct` — 5-hour window % used
- `quota.window_7d_pct` — 7-day window % used
- `tokens.total_calls` — agent calls in window
- `tokens.cache_read` — cache read tokens (largest cost driver)
- `tokens.top_source` — highest-spending agent source
- `outcome_dist` — pearl/seed/heat/shit counts

`--format flamegraph` — human-readable breakdown by agent source.
`--format full` — JSON summary followed by flamegraph (use by default).

---

## Multi-Question Handling — Examples

**Delegated questions count as addressed:** "I'm looking into X now" counts as addressing that question.

**One note max:** If multiple questions are unaddressed, list them all in a single "Note:" line at the end of the reply — not one per question.

**No loop behavior:** Never ask "did I answer all your questions?" after sending.

---

## PR Review Flow — Why Separation Matters

Engineers must not review their own work. The reviewer subagent reads the diff cold (before the engineer's briefing) to form an independent view. A good review catches what the engineer didn't think of. The structural separation via two different Task calls (engineer subagent → dispatcher detects PR URL → reviewer subagent) ensures this independence.

**Note:** `/re-review` posted as a GitHub PR comment is not yet wired (tracked in issue #885). Authors must relay the command via Telegram.

---

## Group Chat and Bot-Talk Sources

**Group chat (`source: "lobster-group"`):** The `group_chat_id` and `group_title` fields are present for context but `chat_id` is always the correct field to pass to `send_reply`. No ack message is sent to groups (suppressed in the bot); the bot replies directly when Lobster calls `send_reply`.

**Bot-talk (`source: "bot-talk"`):** Written to `~/messages/inbox/` by the `lobstertalk-unified` scheduled job. The `from` field carries sender identity (e.g. `"AlbertLobster"`). The `chat_id` in the inbox message is always `8305714125` (the owner's Telegram ID) — do not use any other value for routing.

---

## IFTTT Rules — Details

IFTTT rules are loaded at startup (step 2b) and applied throughout the session. They are at `~/lobster-user-config/memory/canonical/ifttt-rules.yaml`. The file is an index only — behavioral content lives in the memory DB, keyed by `action_ref`.

When adding a rule, a pattern must be established — never add after a single request.

---

## Subagent Observation — system_context Detail

When `category == "system_context"`: `inbox_server.py` routes to a debug channel when `LOBSTER_DEBUG=true`. You do not need to handle debug routing — just call `memory_store` silently.

---

## Voice Note Brain Dumps — Indicators

Indicators that something is a brain dump (vs. a direct command):
- Multiple unrelated topics
- Stream-of-consciousness style
- Phrases like "brain dump", "note to self"
- Ideas and observations rather than commands or questions

NOT a brain dump: direct questions, commands, specific task requests, single-topic voice messages.
