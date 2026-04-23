#!/bin/bash
#===============================================================================
# Lobster Instance-Specific Upgrade Steps
#
# This file is sourced at the end of upgrade.sh. It contains migration steps
# that are specific to this Lobster instance (dcetlin/Lobster) and should NOT
# be sent upstream to SiderealPress/lobster.
#
# Steps are numbered d1, d2, d3, ... to distinguish them from the core
# migration numbering in upgrade.sh. This prevents numbering collisions when
# pulling upstream changes.
#
# When upgrade.sh is merged from upstream, only upgrade.sh changes. Instance-
# specific steps accumulate here without conflict.
#
# Usage: sourced automatically by upgrade.sh if this file exists.
#        The LOBSTER_DIR, WORKSPACE_DIR, and all helper functions (substep,
#        warn, success, info) are inherited from the calling script.
#===============================================================================

# JOBS_FILE is not defined in upgrade.sh (it evaluates to empty, so the
# [ -f "$JOBS_FILE" ] guards in core migrations are all false). Define it
# here so instance-specific job registrations actually run.
JOBS_FILE="$WORKSPACE_DIR/scheduled-jobs/jobs.json"

# migrated counter is inherited from upgrade.sh's run_migrations() scope.
# Instance-specific steps increment it the same way core migrations do.

    # d1: Register daily-metrics scheduled job (issue #122)
    # Delivers a daily 24-hour snapshot of artifact output to Telegram at 07:00 UTC:
    # GitHub issues opened/closed, agent session counts, git activity on ~/lobster/.
    local daily_metrics_task="$WORKSPACE_DIR/scheduled-jobs/tasks/daily-metrics.md"
    if [ ! -f "$daily_metrics_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$daily_metrics_task" << 'DAILY_METRICS_TASK'
# Daily Metrics Report

**Job**: daily-metrics
**Schedule**: Daily at 7:00 AM UTC (`0 7 * * *`)
**GitHub issue**: https://github.com/dcetlin/Lobster/issues/122

## Context

You are running as a scheduled task. Your job is to collect a 24-hour snapshot of
Lobster's artifact output and deliver it to Dan via Telegram.

## Instructions

Run the daily metrics script:

```bash
uv run ~/lobster/scheduled-tasks/daily-metrics.py
```

The script collects:
- GitHub issues opened/closed in the past 24h, plus current open count
- Agent sessions launched, completed, and still running (from agent_sessions.db)
- Git activity on ~/lobster/ — commits, files changed, lines added/removed

It delivers the formatted report to Telegram via a Claude subagent and writes
task output. No further action is needed after running the script.

If the script fails (non-zero exit), note the error in write_task_output with
status="failed" and include the stderr output.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "daily-metrics"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
DAILY_METRICS_TASK
        substep "Created daily-metrics task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["daily-metrics"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["daily-metrics"] = {
                "name": "daily-metrics",
                "schedule": "0 7 * * *",
                "schedule_human": "Daily at 7:00",
                "task_file": "tasks/daily-metrics.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered daily-metrics job in jobs.json (daily at 07:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (daily-metrics added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d2: Register decay-detector scheduled job (issue #137)
    # Pipeline Layer — Mode A: detects frozen intentions in the issue backlog.
    # Runs nightly at 04:00 UTC. Applies/removes 'stale' labels autonomously; never closes issues.
    local decay_task="$WORKSPACE_DIR/scheduled-jobs/tasks/decay-detector.md"
    if [ ! -f "$decay_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$decay_task" << 'DECAY_DETECTOR_TASK'
# Decay Detector

**Job**: decay-detector
**Schedule**: Daily at 4:00 AM UTC (`0 4 * * *`)
**GitHub issue**: https://github.com/dcetlin/Lobster/issues/137

## Context

You are running as a scheduled task. This is the Pipeline Layer — Mode A: Decay Detection.

This script runs every night at 4:00 AM (after the negentropic sweep at 2:00 AM). It is a no-LLM, pure-data pass — your only job is to run the Python script below and report the result.

## Instructions

Run the decay detector script:

```bash
uv run ~/lobster/scheduled-tasks/decay-detector.py
```

The script:
- Checks if today's sweep was a Night 4 (Issues + Memory) sweep — exits silently if not
- Fetches open issues from `dcetlin/Lobster`
- Applies/removes `stale` labels autonomously based on inactivity thresholds
- Detects near-duplicate titles and empty-body issues
- Appends a decay report section to today's sweep file
- Sends a Telegram ping to Dan (chat_id: 8075091586) summarizing findings

No further action is needed after running the script. If the script exits with code 0, all work is done.

If the script fails (non-zero exit), include the error output in write_task_output with status="failed".

## Output

When you complete your task, call `write_task_output` with:
- job_name: "decay-detector"
- output: The script's stdout/stderr output (or "completed successfully" if clean)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
DECAY_DETECTOR_TASK
        substep "Created decay-detector task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["decay-detector"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["decay-detector"] = {
                "name": "decay-detector",
                "schedule": "0 4 * * *",
                "schedule_human": "Daily at 4:00",
                "task_file": "tasks/decay-detector.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered decay-detector job in jobs.json (daily at 04:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (decay-detector added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d3: Register surface-queue-delivery scheduled job (issue #140)
    local surface_queue_task="$WORKSPACE_DIR/scheduled-jobs/tasks/surface-queue-delivery.md"
    if [ ! -f "$surface_queue_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$surface_queue_task" << 'SURFACE_QUEUE_TASK'
# Reflective Surface Queue Delivery

**Job**: surface-queue-delivery
**Schedule**: Daily at 8:00 AM UTC (`0 8 * * *`)
**GitHub issue**: https://github.com/dcetlin/Lobster/issues/140

## Context

You are running as a scheduled task. The reflective-surface-queue accumulates
observations from premise-review, hygiene-review, and oracle agents. Nothing has
ever read it and routed items to Dan. This job does that.

## Instructions

Run the surface queue delivery script:

```bash
uv run ~/lobster/scheduled-tasks/surface-queue-delivery.py
```

The script:
- Reads `~/lobster-workspace/meta/reflective-surface-queue.json`
- Scores undelivered items by source weight, alignment verdict, and age
- Delivers the top 3 items to Dan via Telegram
- Marks delivered items with `delivered: true` and `delivered_at` timestamp
- Archives items older than 14 days (too stale to be actionable)
- Writes a summary to task output

No further action is needed after running the script.

If the script fails (non-zero exit), include the error output in write_task_output
with status="failed".

## Output

When you complete your task, call `write_task_output` with:
- job_name: "surface-queue-delivery"
- output: The script's stdout/stderr output (or summary if clean)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
SURFACE_QUEUE_TASK
        substep "Created surface-queue-delivery task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["surface-queue-delivery"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["surface-queue-delivery"] = {
                "name": "surface-queue-delivery",
                "schedule": "0 8 * * *",
                "schedule_human": "Daily at 8:00",
                "task_file": "tasks/surface-queue-delivery.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered surface-queue-delivery job in jobs.json (daily at 08:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (surface-queue-delivery added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d4: Register lobster-meta scheduled job (issue #141)
    # Runs the lobster-meta drift-detection agent nightly.
    local lobster_meta_task="$WORKSPACE_DIR/scheduled-jobs/tasks/lobster-meta.md"
    if [ ! -f "$lobster_meta_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$lobster_meta_task" << 'LOBSTER_META_TASK'
# Lobster Meta — Nightly Drift Detection

**Job**: lobster-meta
**Schedule**: Daily at 1:00 AM UTC (`0 1 * * *`)
**GitHub issue**: https://github.com/dcetlin/Lobster/issues/141

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

You are running as the lobster-meta drift-detection agent.

**Start here:** Read `~/lobster/.claude/agents/lobster-meta.md` in full. That file
contains your complete operating instructions including epistemic posture, processing
sequence, and output format. Do not proceed until you have read it.

Follow all steps in that file exactly:
1. Read phase-alignment signals
2. Find what doesn't fit (anomalies, silences, contradictions)
3. Check premise-review.md
4. Append findings to ~/lobster-workspace/meta/proposals.md
5. Queue reflective surfaces to ~/lobster-workspace/meta/reflective-surface-queue.json
6. Archive processed signals
7. Exit

## Output

When you complete your task, call `write_task_output` with:
- job_name: "lobster-meta"
- output: One-line summary: signals processed, anomalies found, surfaces queued.
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
LOBSTER_META_TASK
        substep "Created lobster-meta task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["lobster-meta"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["lobster-meta"] = {
                "name": "lobster-meta",
                "schedule": "0 1 * * *",
                "schedule_human": "Daily at 1:00",
                "task_file": "tasks/lobster-meta.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered lobster-meta job in jobs.json (daily at 01:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (lobster-meta added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d5: Register proposals-digest scheduled job (issue #141)
    local proposals_digest_task="$WORKSPACE_DIR/scheduled-jobs/tasks/proposals-digest.md"
    if [ ! -f "$proposals_digest_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$proposals_digest_task" << 'PROPOSALS_DIGEST_TASK'
# Proposals Digest

**Job**: proposals-digest
**Schedule**: Daily at 3:00 AM UTC (`0 3 * * *`)
**GitHub issue**: https://github.com/dcetlin/Lobster/issues/141

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

Run the proposals digest script:

```bash
uv run ~/lobster/scheduled-tasks/proposals-digest.py
```

The script:
- Reads `~/lobster-workspace/meta/proposals.md`
- Finds entries that have not yet been delivered (no delivered marker)
- Delivers up to 2 entries to Dan via the Lobster inbox (Telegram)
- Marks delivered entries with `<!-- proposals-digest-delivered: YYYY-MM-DD -->` so
  they are not re-sent on subsequent runs

No further action is needed after running the script.

If the script fails (non-zero exit), include the error output in write_task_output
with status="failed".

## Output

When you complete your task, call `write_task_output` with:
- job_name: "proposals-digest"
- output: The script's stdout output (or summary if clean)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
PROPOSALS_DIGEST_TASK
        substep "Created proposals-digest task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["proposals-digest"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["proposals-digest"] = {
                "name": "proposals-digest",
                "schedule": "0 3 * * *",
                "schedule_human": "Daily at 3:00",
                "task_file": "tasks/proposals-digest.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered proposals-digest job in jobs.json (daily at 03:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (proposals-digest added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d6: Register auto-router scheduled job (nightly gate judgment for reflective-surface-queue)
    local auto_router_task="$WORKSPACE_DIR/scheduled-jobs/tasks/auto-router.md"
    if [ ! -f "$auto_router_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$auto_router_task" << 'AUTO_ROUTER_TASK'
# Reflective Surface Queue Auto-Router

**Job**: auto-router
**Schedule**: Nightly at 3:00 AM UTC (`0 3 * * *`)

## Context

You are running as a scheduled task. The reflective-surface-queue accumulates
observations from premise-review, hygiene-review, and other agents. This job
applies a gate judgment to each unrouted item:

- **implementation-ready**: item describes a concrete, scoped change → dispatches
  a functional-engineer via subagent_result inbox message
- **design-open**: item has unresolved premises or requires directional input →
  surfaces to Dan via subagent_result inbox message asking for go/nogo

Gate criterion: can you state in one concrete sentence what the output should be?
If yes → implementation-ready. If no → design-open.

## Instructions

Run the auto-router script:

```bash
uv run ~/lobster/scheduled-tasks/auto-router.py
```

The script:
- Reads the reflective-surface-queue.json (hygiene/meta or meta location)
- Applies gate judgment to each unrouted item
- Writes subagent_result inbox messages for the dispatcher to route
- Marks each item with routed_at and route_decision
- Writes a summary to task output

No further action is needed after running the script.

If the script fails (non-zero exit), include the error output in write_task_output
with status="failed".

## Output

When you complete your task, call `write_task_output` with:
- job_name: "auto-router"
- output: The script's stdout/stderr output (or summary if clean)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
AUTO_ROUTER_TASK
        substep "Created auto-router task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["auto-router"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["auto-router"] = {
                "name": "auto-router",
                "schedule": "0 3 * * *",
                "schedule_human": "Nightly at 3:00 UTC",
                "task_file": "tasks/auto-router.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered auto-router job in jobs.json (nightly at 03:00 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (auto-router added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d7: Update bot-talk poller task files to mirror both sides of conversation
    # The poller previously only forwarded AlbertLobster messages to the owner. The updated
    # task files instruct the poller to collect both SaharLobster and AlbertLobster
    # messages, sort them chronologically, and send a single conversation block to the owner.
    local bt_poller_src="$LOBSTER_DIR/scheduled-tasks/tasks/bot-talk-poller.md"
    local bt_fast_src="$LOBSTER_DIR/scheduled-tasks/tasks/bot-talk-poller-fast.md"
    local bt_tasks_dir="$WORKSPACE_DIR/scheduled-jobs/tasks"
    if [ -f "$bt_poller_src" ] && [ -d "$bt_tasks_dir" ]; then
        cp "$bt_poller_src" "$bt_tasks_dir/bot-talk-poller.md"
        substep "Updated bot-talk-poller.md to mirror both conversation sides"
        migrated=$((migrated + 1))
    fi
    if [ -f "$bt_fast_src" ] && [ -d "$bt_tasks_dir" ]; then
        cp "$bt_fast_src" "$bt_tasks_dir/bot-talk-poller-fast.md"
        substep "Updated bot-talk-poller-fast.md to mirror both conversation sides"
        migrated=$((migrated + 1))
    fi

    # d8: WOS orchestration layer — issue-sweeper, vision_ref schema, route_reason normalization,
    # and numbered DB migrations (WOS Phase 1, issue #167)
    # Creates the orchestration directory, registers the issue-sweeper nightly job (03:30 UTC),
    # and runs all unapplied schema migrations on the WOS registry DB.
    local orchestration_dir="$WORKSPACE_DIR/orchestration"
    local sweeper_task="$WORKSPACE_DIR/scheduled-jobs/tasks/issue-sweeper.md"
    if [ ! -d "$orchestration_dir" ]; then
        mkdir -p "$orchestration_dir"
        substep "Created $orchestration_dir for WOS registry"
        migrated=$((migrated + 1))
    fi
    if [ ! -f "$sweeper_task" ]; then
        mkdir -p "$WORKSPACE_DIR/scheduled-jobs/tasks"
        cat > "$sweeper_task" << 'ISSUE_SWEEPER_TASK'
# Issue Sweeper — WOS Phase 1

**Job**: issue-sweeper
**Schedule**: Nightly at 3:30 AM UTC (`30 3 * * *`)
**Created**: WOS Phase 1 — issue #167

## Context

You are the WOS Issue Sweeper running as a scheduled task. Your job is to scan
the dcetlin/Lobster GitHub issue backlog and create proposed Units of Work (UoWs)
in the Registry for issues that are ready-to-execute or need attention.

The Registry CLI is at `~/lobster/src/orchestration/registry_cli.py`.
The database lives at `~/lobster-workspace/orchestration/registry.db`.

## Instructions

### 0. Load Vision Object

Read `~/lobster-user-config/vision.yaml` at the start of each sweep. Extract:
- `current_focus.this_week.primary` — the primary focus for this week
- `current_focus.what_not_to_touch` — list of domains to exclude from UoW proposals
- `active_project.phase_intent` — the current phase intent (one paragraph)

Use these to populate `vision_ref` when upserting UoWs (see step 4). If the file
does not exist, log a warning in sweep output and continue without vision anchoring.

### 1. Expire stale proposals

First, expire any proposals older than 14 days that have not been confirmed:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py expire-proposals
```

Include the result in your sweep output.

### 2. Check for stale-active UoWs

Check whether any active UoWs have their source issue closed:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py check-stale
```

If any stale UoWs are found, include them in the sweep output and flag for Dan's review.

### 3. Scan the GitHub issue backlog

Fetch open issues from dcetlin/Lobster:

```bash
gh issue list --repo dcetlin/Lobster --state open --json number,title,labels,createdAt,updatedAt,comments --limit 100
```

For each issue, apply the following criteria:

**Propose as UoW if ANY of the following are true:**
- Has `ready-to-execute` label AND no linked PR AND age > 3 days
- Has `high-priority` label AND no recent comment (>7 days) AND no linked PR
- Open > 14 days AND no `on-hold` label AND no `needs-design` label AND no `stale` label AND no linked PR

**Skip if:**
- Has `on-hold` label (note in "Dan-blocked" section)
- Has `needs-design` label (not ready for execution)
- Has `stale` label already
- Has an open linked PR (work in progress)
- Issue title/domain matches an entry in `vision.current_focus.what_not_to_touch`
  (note as "excluded by vision.what_not_to_touch" in sweep output)

### 4. Create UoWs for qualifying issues

For each qualifying issue, upsert a proposed UoW:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py upsert \
  --issue <N> \
  --title "<issue title>" \
  --sweep-date "$(date +%Y-%m-%d)"
```

After upserting, write the `vision_ref` field using the Registry CLI or direct
SQLite update. Determine the vision anchor by checking:

1. If the issue title or content matches `current_focus.this_week.primary`:
   vision_ref = {"layer": "current_focus", "field": "this_week.primary",
                 "statement": "<verbatim primary text>", "anchored_at": "<now ISO>"}

2. If the issue is structural/registry/substrate work (relates to WOS, routing,
   or Vision Object itself): vision_ref references `active_project.phase_intent`.

3. Otherwise: vision_ref = null (issue has no explicit vision anchor yet).

Record the result (inserted vs skipped with reason, and vision_ref assigned) in
your sweep output.

### 5. Compute gate readiness

Check Phase 1 → Phase 2 autonomy gate status:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py gate-readiness
```

Include the output in your sweep report.

### 6. Build the ready queue

Query pending and proposed UoWs from the registry:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py list --status proposed
uv run ~/lobster/src/orchestration/registry_cli.py list --status pending
```

Order by created_at (oldest first). Distinguish:
- `proposed` items: labeled "awaiting /confirm" — Dan must run `/confirm <uow-id>` to activate
- `pending` items: labeled "confirmed, awaiting execution"

Flag any `proposed` records approaching 14-day expiry (>= 12 days old) with:
"expiring soon — confirm or this proposal will expire in N days"

### 7. Write sweep output

Call `write_task_output` with a structured report containing:

1. **Vision Object loaded**: yes/no (if no, reason)
2. **Current focus**: one-line summary from vision.current_focus.this_week.primary
3. **Expired proposals**: count and ids
4. **Stale-active UoWs**: list (id, issue, summary) — if any
5. **Issues scanned**: count
6. **UoWs created**: list (id, issue number, title, action: inserted/skipped, vision_ref: layer or null)
7. **Vision-excluded issues**: issues skipped due to what_not_to_touch
8. **Ready queue** (proposed — awaiting /confirm):
   - One line per UoW: `<id> | #<issue> | <title> | created: <date> [EXPIRING SOON]`
9. **Confirmed queue** (pending — awaiting execution):
   - One line per UoW: `<id> | #<issue> | <title>`
10. **Dan-blocked items**: issues with `on-hold` label
11. **Gate readiness**: gate_met, days_running, ratio

Keep it concise — Dan reads this on mobile.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "issue-sweeper"
- output: Your structured sweep report
- status: "success" or "failed"
ISSUE_SWEEPER_TASK
        substep "Created issue-sweeper task file in scheduled-jobs/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["issue-sweeper"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local now_iso
            now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$now_iso" '.jobs["issue-sweeper"] = {
                "name": "issue-sweeper",
                "schedule": "30 3 * * *",
                "schedule_human": "Nightly at 3:30 UTC",
                "task_file": "tasks/issue-sweeper.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered issue-sweeper job in jobs.json (nightly at 03:30 UTC)"
            if [ -x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                bash "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab synchronized (issue-sweeper added)"
            fi
            migrated=$((migrated + 1))
        fi
    fi

    # d8a: Add vision_ref column to uow_registry (Vision Object Phase 1)
    # Adds the intent-anchor field that connects each UoW back to a specific
    # Vision Object layer. NULL is correct for pre-existing rows.
    local registry_db="$WORKSPACE_DIR/orchestration/registry.db"
    if [ -f "$registry_db" ]; then
        if ! python3 -c "
import sqlite3, sys
conn = sqlite3.connect('$registry_db')
cols = [row[1] for row in conn.execute('PRAGMA table_info(uow_registry)').fetchall()]
conn.close()
sys.exit(0 if 'vision_ref' in cols else 1)
" 2>/dev/null; then
            sqlite3 "$registry_db" "ALTER TABLE uow_registry ADD COLUMN vision_ref TEXT DEFAULT NULL;" 2>/dev/null && \
                substep "Added vision_ref column to uow_registry" || \
                warn "Could not add vision_ref column to uow_registry (non-fatal: column added at schema init for new installs)"
            migrated=$((migrated + 1))
        fi
    fi

    # d8b: Normalize abbreviated legacy route_reason rows in uow_registry.
    # Canonical value is "phase1-default: no classifier" — updates abbreviated rows.
    local registry_db="$WORKSPACE_DIR/orchestration/registry.db"
    if [ -f "$registry_db" ]; then
        local abbrev_count
        abbrev_count=$(uv run python3 -c "
import sqlite3
conn = sqlite3.connect('$registry_db')
n = conn.execute(\"SELECT COUNT(*) FROM uow_registry WHERE route_reason = 'phase1-default'\").fetchone()[0]
print(n)
conn.close()
" 2>/dev/null || echo "0")
        if [ "$abbrev_count" -gt 0 ]; then
            uv run python3 -c "
import sqlite3
conn = sqlite3.connect('$registry_db')
conn.execute(\"UPDATE uow_registry SET route_reason = 'phase1-default: no classifier' WHERE route_reason = 'phase1-default'\")
conn.commit()
conn.close()
" 2>/dev/null && \
                substep "Normalized $abbrev_count abbreviated route_reason row(s) to canonical value" || \
                warn "Could not normalize abbreviated route_reason rows (non-fatal)"
            migrated=$((migrated + 1))
        fi
    fi

    # d8c: Run numbered DB migrations (WOS orchestration layer)
    # src/orchestration/migrate.py applies all unapplied numbered .sql files
    # from src/orchestration/migrations/ to the registry DB. Idempotent.
    local registry_db="$WORKSPACE_DIR/orchestration/registry.db"
    if [ -f "$registry_db" ]; then
        if uv run "$LOBSTER_DIR/src/orchestration/migrate.py" "$registry_db" 2>/dev/null; then
            substep "WOS DB migrations applied (or already up to date)"
            migrated=$((migrated + 1))
        else
            warn "WOS DB migration runner failed — run manually: uv run src/orchestration/migrate.py $registry_db"
        fi
    fi

    # d9: Add LOBSTER-GARDEN-CARETAKER cron entry and jobs.json registration
    # (GardenCaretaker PR4, WOS Phase 2).
    # Replaces cultivator.py + issue-sweeper.py with a unified scan-and-tend loop.
    # Runs every 15 minutes as a Type C cron-direct script (not LLM-dispatched).
    local GARDEN_CARETAKER_MARKER="# LOBSTER-GARDEN-CARETAKER"
    if ! crontab -l 2>/dev/null | grep -q "$GARDEN_CARETAKER_MARKER"; then
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$GARDEN_CARETAKER_MARKER" \
            "*/15 * * * * cd $HOME && uv run $LOBSTER_DIR/scheduled-tasks/garden-caretaker.py >> $WORKSPACE_DIR/logs/garden-caretaker.log 2>&1 $GARDEN_CARETAKER_MARKER"
        substep "Added garden-caretaker cron entry (garden-caretaker.py, every 15 min)"
        migrated=$((migrated + 1))
    else
        substep "garden-caretaker crontab entry already present"
    fi
    local GARDEN_JOBS_FILE="$WORKSPACE_DIR/scheduled-jobs/jobs.json"
    if [ -f "$GARDEN_JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["garden-caretaker"]' "$GARDEN_JOBS_FILE" > /dev/null 2>&1; then
            local gc_now_iso
            gc_now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$gc_now_iso" '.jobs["garden-caretaker"] = {
                "name": "garden-caretaker",
                "schedule": "*/15 * * * *",
                "schedule_human": "Every 15 minutes",
                "task_file": null,
                "created_at": $now,
                "updated_at": $now,
                "enabled": true,
                "type": "C",
                "dispatch": "cron-direct"
            }' "$GARDEN_JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$GARDEN_JOBS_FILE"
            substep "Registered garden-caretaker in jobs.json (Type C, every 15 min)"
            migrated=$((migrated + 1))
        fi
    fi

    # d10: Ensure ~/messages/config/group-whitelist.json exists.
    # The group chat gating system (Phases 1-4) reads this file at startup.
    # (Upstream Migration 62 covers this on SiderealPress installs; this entry
    # handles dcetlin/Lobster where Migration 62 was taken by garden-caretaker.)
    local MESSAGES_CONFIG_DIR="$HOME/messages/config"
    if [ ! -d "$MESSAGES_CONFIG_DIR" ]; then
        mkdir -p "$MESSAGES_CONFIG_DIR"
        substep "Created $MESSAGES_CONFIG_DIR"
        migrated=$((migrated + 1))
    fi
    if [ ! -f "$MESSAGES_CONFIG_DIR/group-whitelist.json" ]; then
        echo '{"groups": {}}' > "$MESSAGES_CONFIG_DIR/group-whitelist.json"
        substep "Created empty $MESSAGES_CONFIG_DIR/group-whitelist.json"
        migrated=$((migrated + 1))
    fi

    # d11: Register learnings-proposals scheduled job
    # Weekly job that reads oracle/learnings.md entries from the past week and
    # appends a new proposal entry to ~/lobster-workspace/meta/proposals.md.
    # proposals-digest.py handles delivery; this job only writes.
    local learnings_proposals_task="$LOBSTER_DIR/scheduled-tasks/tasks/learnings-proposals.md"
    if [ ! -f "$learnings_proposals_task" ]; then
        mkdir -p "$LOBSTER_DIR/scheduled-tasks/tasks"
        cat > "$learnings_proposals_task" << 'LEARNINGS_PROPOSALS_TASK'
# Learnings Proposals

**Job**: learnings-proposals
**Schedule**: Weekly on Sundays at 4:00 AM UTC (`0 4 * * 0`)
**Created**: 2026-04-13

## Context

You are running as a scheduled task. Your purpose is to read the week's oracle learnings and generate actionable proposals for `meta/proposals.md`.

## Instructions

### 1. Read oracle/learnings.md

Read `~/lobster-workspace/oracle/learnings.md` (Layer 2 archive section preferred; fall back to Layer 1 index if Layer 2 is empty).

Filter to entries dated within the past 7 days. If no entries exist in that window, write a task output noting "No new learnings this week — no proposal generated" and exit.

### 2. Read existing proposals

Read `~/lobster-workspace/meta/proposals.md` to understand existing proposals and avoid duplicating themes already covered.

### 3. Generate a proposal

Based on the week's learnings, write a proposal that answers: "Given what oracle review surfaced this week, what concrete system improvement would address the underlying pattern?"

Requirements for the proposal:
- Must be actionable (a specific change, not a vague suggestion)
- Must reference the learning(s) that motivated it (by date and PR number)
- Must state what the expected outcome would be if implemented

### 4. Append to proposals.md

Append a new entry to `~/lobster-workspace/meta/proposals.md` in this exact format:

```
### [YYYY-MM-DD] Learnings-driven: <short title>

**Signals processed:** <count of learnings entries from this week>

**Source learnings:**
- [YYYY-MM-DD] PR #NNN — <learning summary>

**Proposal:** <the concrete proposal>

**Expected outcome:** <what changes if this is implemented>
```

Use today's date for the heading. Do not add a delivered marker — the `proposals-digest` job handles delivery.

## Output

Call `write_task_output` with:
- job_name: "learnings-proposals"
- output: Summary of what was written (or why nothing was written)
- status: "success" or "failed"
LEARNINGS_PROPOSALS_TASK
        substep "Created learnings-proposals task file in scheduled-tasks/tasks/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$JOBS_FILE" ] && command -v jq >/dev/null 2>&1; then
        if ! jq -e '.jobs["learnings-proposals"]' "$JOBS_FILE" > /dev/null 2>&1; then
            local lp_now_iso
            lp_now_iso=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N+00:00")
            TMP_JOBS=$(mktemp)
            jq --arg now "$lp_now_iso" '.jobs["learnings-proposals"] = {
                "name": "learnings-proposals",
                "schedule": "0 4 * * 0",
                "schedule_human": "Weekly on Sundays at 4:00 AM UTC",
                "task_file": "tasks/learnings-proposals.md",
                "created_at": $now,
                "updated_at": $now,
                "enabled": true
            }' "$JOBS_FILE" > "$TMP_JOBS" && mv "$TMP_JOBS" "$JOBS_FILE"
            substep "Registered learnings-proposals job in jobs.json (weekly Sundays at 04:00 UTC)"
            migrated=$((migrated + 1))
        fi
    fi
    local LP_MARKER="# LOBSTER-SCHEDULED-LEARNINGS-PROPOSALS"
    if ! crontab -l 2>/dev/null | grep -q "$LP_MARKER"; then
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$LP_MARKER" \
            "0 4 * * 0 $LOBSTER_DIR/scheduled-tasks/dispatch-job.sh learnings-proposals $LP_MARKER"
        substep "Added learnings-proposals cron entry (weekly Sundays at 04:00 UTC)"
        migrated=$((migrated + 1))
    else
        substep "learnings-proposals crontab entry already present"
    fi

    # d12: Update garden-caretaker cron schedule from every 15 minutes to every 2 hours.
    # The 15-minute sweep caused bulk germination of 150+ UoWs at once when GitHub issues
    # were scanned on every cycle. A 2-hour cadence spreads the load without losing coverage.
    local GC_MARKER="# LOBSTER-GARDEN-CARETAKER"
    local GC_OLD_SCHEDULE="*/15 \* \* \* \*"
    local GC_NEW_SCHEDULE="0 \*/2 \* \* \*"
    if crontab -l 2>/dev/null | grep -q "$GC_MARKER"; then
        if crontab -l 2>/dev/null | grep "$GC_MARKER" | grep -q "^\*/15"; then
            # Remove old 15-min entry and add new 2-hour entry
            "$LOBSTER_DIR/scripts/cron-manage.sh" remove "$GC_MARKER"
            "$LOBSTER_DIR/scripts/cron-manage.sh" add "$GC_MARKER" \
                "0 */2 * * * cd \$HOME && uv run $LOBSTER_DIR/scheduled-tasks/garden-caretaker.py >> $WORKSPACE_DIR/logs/garden-caretaker.log 2>&1 $GC_MARKER"
            substep "Updated garden-caretaker cron: every 15 min → every 2 hours (d12)"
            migrated=$((migrated + 1))
        else
            substep "garden-caretaker cron already updated — skipping d12"
        fi
    else
        substep "garden-caretaker cron entry not found — skipping d12"
    fi

