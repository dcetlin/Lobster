# scheduled-tasks/

Scripts in this directory are called **only by cron**. They are never invoked by
agents directly.

Agents interact with scheduling via MCP tools (`create_scheduled_job`,
`update_scheduled_job`). Those tools manage `jobs.json` and the task files under
`scheduled-jobs/tasks/`. This directory holds the shell scripts that cron calls
to do lightweight pre-checks before (optionally) writing to the inbox.

---

## The Two-Layer Model

```
Cron
 │
 └─ Layer 1: shell script (this directory)
     │  Lightweight check — HTTP call, file stat, API poll
     │  If nothing new: log, exit 0 (no LLM cost)
     │  If new data: call dispatch-job.sh → write to inbox
     │
     └─ Layer 2: LLM subagent (spawned by dispatcher)
         Receives pre-filtered payload
         Does reasoning, formatting, notifications, GitHub actions
         Updates state file on success
```

**Key invariant:** No LLM cost when nothing changed. A polling script that fires
every 2 minutes and finds nothing new exits 0 and costs nothing.

For full architecture documentation, see issue #1059.

---

## Layer 2 Execution Modes

When the dispatcher spawns a Layer 2 subagent, the subagent can execute in one
of two modes. Both are triggered identically (via `dispatch-job.sh` → inbox →
`scheduled_reminder` message). The difference is in how the `.md` task brief is
written.

### Mode A: Pure-dispatch (agent-only)

The `.md` task brief in `scheduled-jobs/tasks/<job-name>.md` contains all
instructions in natural language. The subagent reasons over them and calls MCP
tools directly — no Python script is involved.

```
dispatch-job.sh → inbox → dispatcher → subagent reads task brief → MCP tools
```

**Examples:** `morning-briefing`, `issue-sweeper`, `negentropic-sweep`,
`uow-reflection`, `async-deep-work`, `lobster-hygiene`.

Use this mode when the job logic is best expressed as agent reasoning: fetching
data via MCP, writing summaries, updating GitHub issues, sending Telegram
messages.

### Mode B: Script-assisted (agent calls Python)

The `.md` task brief instructs the subagent to run a Python script in
`scheduled-tasks/` via `uv run`. The script does the heavy data work; the
subagent wraps it with delivery and logging.

```
dispatch-job.sh → inbox → dispatcher → subagent reads task brief
  → uv run scheduled-tasks/<job>.py → subagent delivers result via MCP
```

**Examples:** `pattern-candidate-sweep` (runs `pattern-candidate-sweep.py`),
`weekly-epistemic-retro` (runs `weekly-epistemic-retro.py`).

Use this mode when the job requires deterministic data processing that is easier
to test and version as Python code, but the delivery and logging still benefit
from agent judgment.

> **Note:** The Python scripts under `scheduled-tasks/` that share a name with a
> job (e.g., `pattern-candidate-sweep.py`) are **task executors**, not Layer 1
> scripts. They are invoked by the subagent at Layer 2, not by cron. Do not add
> a direct cron entry for them — wire them through `dispatch-job.sh` instead.

### Mode C: Local-code (no LLM)

Some scripts bypass the dispatch path entirely. Cron calls them directly and
they perform self-contained maintenance work with no LLM involvement. These use
the `local-code` template.

```
Cron → script → done  (no inbox write, no subagent)
```

**Examples:** `export-logs.py`, `sync-crontab.sh`, `transcription-monitor.py`.

> **Delivery note for `transcription-monitor.py`:** This script writes directly
> to `~/messages/outbox/` rather than routing through the inbox. See
> [Delivery Convention Exception](#delivery-convention-exception) below.

---

## Scripts in This Directory

| Script | Mode | Description |
|---|---|---|
| `dispatch-job.sh` | — | Core dispatcher: writes `scheduled_reminder` to inbox; used by Layer 1 scripts and called directly by cron for non-polling jobs |
| `bot-talk-check-dispatch.sh` | Layer 1 poll-with-state | Polls bot-talk API; skips dispatch if no new messages since last cursor |
| `lobstertalk-incoming-check.sh` | Layer 1 API poll | Polls bot-talk API for messages addressed to this Lobster instance; skips dispatch if none |
| `sync-crontab.sh` | Mode C (local-code) | Syncs crontab from config; no LLM, no inbox write |
| `export-logs.py` | Mode C (local-code) | Exports log data; no LLM, no inbox write |
| `file-size-monitor.py` | Mode C (local-code) | Checks bootup/config file line counts; files GitHub issue if threshold exceeded; no LLM, no inbox write |
| `transcription-monitor.py` | Mode C (local-code) | Progress pings during transcription; writes to outbox directly (see Delivery Convention Exception) |
| `pattern-candidate-sweep.py` | Mode B task executor | Called by the `pattern-candidate-sweep` subagent; not a cron script |
| `weekly-epistemic-retro.py` | Mode B task executor | Called by the `weekly-epistemic-retro` subagent; not a cron script |
| `daily-metrics.py` | Mode B task executor | Called by a subagent task brief; not a cron script |

### `dispatch-job.sh`

The core dispatcher. Reads the task file for a job, writes a `scheduled_reminder`
message to `~/messages/inbox/`, and updates `jobs.json` with the last-run
timestamp. All Layer 1 polling scripts `exec` into this when they find new data.

For non-polling jobs (reminders, digests, mode-switches), cron calls this
directly:
```
0 8 * * 1-5  ~/lobster/scheduled-tasks/dispatch-job.sh morning-briefing
```

### `post-reminder.sh` (in `scripts/`)

Purpose-built for **self-reminders** created by the dispatcher itself (e.g.,
"remind me in 30 minutes"). Has built-in dedup: checks `inbox/` and
`processing/` for an existing unprocessed message of the same type before
writing. Safe to call from cron or from `at`.

---

## Delivery Convention Exception

All scheduled jobs that produce user-facing messages follow the standard delivery
path: write to inbox → dispatcher picks up → subagent sends via `send_reply`.

**`transcription-monitor.py` is the one documented exception.** It writes
progress pings directly to `~/messages/outbox/`, bypassing the inbox. This is
intentional: transcription runs synchronously on the machine and the user needs
real-time progress feedback. The inbox polling interval (up to several seconds)
would introduce noticeable lag in pings that are already time-sensitive. The
connector picks up outbox files immediately, so direct outbox writes give
near-instant delivery.

If you encounter another script that writes to outbox directly without this
justification, treat it as unintentional drift and route it through the inbox
instead.

---

## Adding a New Job

1. Pick a pattern from `templates/README.md`.
2. Copy the appropriate template, fill in placeholders, save to this directory.
3. Create `~/lobster-workspace/scheduled-jobs/tasks/<job-name>.md` — this is
   the task brief passed to the LLM subagent.
4. Register the job via the `create_scheduled_job` MCP tool (this updates
   `jobs.json` and the crontab) **or** add an entry to `sync-crontab.sh`.

---

## What Belongs Here vs. Elsewhere

| If you need to... | Do this |
|---|---|
| Poll an external source on a schedule | Create a Layer 1 script here |
| Dispatch a non-polling job on a schedule | Call `dispatch-job.sh` from cron |
| Schedule a self-reminder from within the dispatcher | Use `post-reminder.sh` |
| Run something with no LLM involvement | Write a `local-code` script here |
| Register or configure a scheduled job | Use the `create_scheduled_job` MCP tool |
| See patterns and conventions | Read `templates/README.md` |
