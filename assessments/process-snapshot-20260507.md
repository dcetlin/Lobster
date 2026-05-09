# Process Snapshot — 2026-05-07

**Generated:** 2026-05-07T00:14:33Z  
**Purpose:** Document active Lobster/Claude processes before clean restart to address API usage consumption issue.

---

## Running Processes

| PID | Started | CPU | Command | Category |
|-----|---------|-----|---------|----------|
| 840 | May01 | 0.0% | `/home/lobster/lobster/.venv/bin/python .../src/transcription/worker.py` | Infrastructure (transcription worker) |
| 842 | May01 | 0.0% | `/home/lobster/lobster/.venv/bin/python .../src/daemons/wos_execute_router.py` | Infrastructure (WOS router daemon) |
| 997 | May01 | 0.0% | `/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown` | System (root, OS upgrade watcher) |
| 1016 | May01 | 0.0% | `/usr/lib/systemd/systemd --user` | System (user systemd) |
| 4626 | May01 | 0.0% | `/home/lobster/lobster/.venv/bin/python3 .../src/dashboard/server.py --host 0.0.0.0 --port 9100` | Infrastructure (dashboard server) |
| 256579 | May02 | 0.0% | `/usr/bin/dbus-daemon --session` | System (session bus) |
| 474896 | May02 | 0.0% | `/usr/bin/tmux -L lobster new-session ...` | Infrastructure (tmux session manager) |
| 474897 | May02 | 0.0% | `/bin/bash .../scripts/claude-persistent.sh` | Infrastructure (Claude restart wrapper) |
| 4050750 | May06 | 0.0% | `/home/lobster/lobster/.venv/bin/python .../src/bot/lobster_bot.py` | Infrastructure (Telegram bot) |
| **22047** | **00:10** | **3.6%** | **`claude --dangerously-skip-permissions --model sonnet ...`** | **Dispatcher (main loop — DO NOT KILL)** |
| 22048 | 00:10 | 0.0% | `tee -a .../logs/claude-session.log` | Infrastructure (log tee for dispatcher) |
| 22061 | 00:10 | 1.0% | `/home/lobster/lobster/.venv/bin/python .../src/mcp/inbox_server.py` | Infrastructure (MCP inbox server) |

### Observation
There is **only one Claude process** (PID 22047) — the dispatcher itself. No orphaned Claude Code subagent processes were found. This is a clean process state.

---

## Running Systemd Services

| Service | Status |
|---------|--------|
| `lobster-claude.service` | active/running — Lobster Claude main loop |
| `lobster-router.service` | active/running — Telegram to Claude Code bridge |
| `lobster-transcription.service` | active/running — whisper.cpp voice-to-text pipeline |
| `lobster-wos-router.service` | active/running — WOS execute router daemon |

---

## Stuck Processing Messages

**None found.** `~/messages/processing/` directory is empty.

---

## Stale In-Flight Work (inflight-work.jsonl)

The inflight-work.jsonl file contains **59 tasks marked "running" that have no corresponding completion record**. These are all stale — the processes that would have completed them died in previous sessions.

Date range of stale entries: 2026-04-25 through 2026-05-07 (current process-cleanup task)

Notable stale task clusters:
- **April 25-26:** WOS force-resets, negentropic sweep cron fixes, upstream merge recovery
- **April 26-27:** WOS escalation wave (diagnosing_orphan cycle 4 — multiple UoWs)
- **April 27:** PR merges (#986, #987, #988), morning briefing, github-issue-cultivator
- **May 1:** WOS observability swarm (4 parallel implementation agents)
- **May 4:** Queue token usage, toxicity answer
- **May 7:** process-cleanup (current task, in progress)

---

## Active Workstreams

All workstreams are directories (not process-backed): `~/lobster-workspace/workstreams/`

Key active workstreams (non-archived):
- async-deep-work
- docs
- epistemic-principles
- first-principles
- issue-lifecycle-worker
- linear-migration
- lobster-core
- lobster-system
- negentropic-sweep
- personal-todo-system
- phase-reference / phase-reference-architecture
- philosophy / philosophy-explore
- prescription-audit-v2
- semantic-mirror
- town-square
- upstream-precision-merge
- usage-observability
- vision-object
- wos (main WOS workstream)
- Various WOS UoW workstreams (uow_20260502_*)

---

## Actions Taken

### Processes Killed
**None killed.** No orphaned Claude Code subagent processes were found. The only Claude process (PID 22047) is the dispatcher itself, which must not be killed.

### Processes Preserved
All infrastructure processes preserved:
- Telegram bot (PID 4050750)
- MCP inbox server (PID 22061)
- WOS execute router (PID 842)
- Transcription worker (PID 840)
- Dashboard server (PID 4626)
- Dispatcher / main loop (PID 22047)

### Stale Inflight Work
The inflight-work.jsonl contains 59 stale "running" entries. These are log artifacts from previous sessions where subagents died without writing completion records. They do not represent active processes — they are historical records only. No cleanup is required for process hygiene; they can be cleared if desired by truncating the file to only completed entries.

---

## Root Cause Assessment

The API usage consumption pattern Dan described ("lobster sleeps, lobster wakes, then it all gets consumed so quickly") is likely caused by:

1. **Context compaction effects:** After a long sleep, the dispatcher wakes into a compacted context and may trigger multiple catch-up subagents simultaneously.
2. **Stale inflight-work.jsonl:** The reconciler or dispatcher may interpret old "running" entries as active tasks needing recovery/re-dispatch.
3. **WOS executor:** If execution_enabled is true, the wos-execute-router may dispatch multiple UoWs in rapid succession after a session restart.
4. **Scheduled jobs burst:** Multiple scheduled jobs may fire close together after a long sleep.

**Recommendation:** After clean restart, check `wos-config.json` execution_enabled state and consider setting it false until the consumption pattern stabilizes.
