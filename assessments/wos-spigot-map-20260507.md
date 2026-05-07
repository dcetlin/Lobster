# WOS Spigot Map — 2026-05-07

**Context**: WOS has been paused (`execution_enabled=false` in `wos-config.json`) since ~2026-05-04. This document maps all scheduled jobs against WOS dependency and records which were disabled as part of the pause.

---

## Full Job Inventory

| Job | WOS-specific | Was Enabled | Cadence | Action Taken |
|-----|-------------|-------------|---------|--------------|
| **executor-heartbeat** | Yes — dispatches UoWs for execution | Yes | Every 3 min | **Disabled** (jobs.json) |
| **steward-heartbeat** | Yes — runs WOS steward pass per UoW | Yes | Every 3 min | **Disabled** (jobs.json) |
| **issue-sweeper** | Yes — sweeps GitHub issues, upserts UoWs to registry | Yes | Every 30 min, 22:00–05:00 UTC | **Disabled** (systemd + jobs.json) |
| **uow-reflection** | Yes — summarizes overnight WOS pipeline activity, reads registry | Yes | Daily at 06:30 UTC | **Disabled** (systemd + jobs.json) |
| **pattern-candidate-sweep** | Yes — surfaces meta-thread candidates from WOS session patterns | Yes | Every Wed at 08:00 UTC | **Disabled** (systemd + jobs.json) |
| **github-issue-cultivator** | Yes — cultivates GitHub issues that feed WOS intake | Yes | Daily at 06:00 UTC | **Disabled** (systemd + jobs.json) |
| **proposals-authorship** | Yes — reviews oracle learnings and authors WOS proposals | Yes | Every Mon at 09:00 UTC | **Disabled** (jobs.json) |
| **wos-overnight-loop** | Yes — observes and coordinates WOS overnight test cycle | Yes | Every 30 min | **Disabled** (systemd + jobs.json) |
| **wos-hourly-observation** | Yes — hourly WOS pipeline observation during working hours | No (jobs.json already false) | 06:00–12:00 UTC hourly | **Disabled** (systemd — was already false in jobs.json) |
| **wos-queue-monitor** | Yes — monitors WOS backlog depth (starvation/toxicity) | Yes | Every 30 min | **Disabled** (jobs.json) |
| **wos-health-check** | Yes — flags UoWs stuck in pipeline states >24h/48h | Yes | Every 6 hours | **Disabled** (jobs.json) |
| **wos-metabolic-digest** | Yes — daily digest classifying UoWs as pearl/heat/seed/shit | Yes | Daily at 09:00 UTC | **Disabled** (jobs.json) |
| **wos-pr-sweeper** | Yes — scans WOS-associated PRs for stale/unacknowledged merges | Yes | Every 6 hours | **Disabled** (jobs.json) |
| **wos-health-monitor** | Yes — 5-min heartbeat health monitor for WOS pipeline | Yes | Every 5 min | **Disabled** (systemd) |
| **morning-briefing** | No — daily briefing, independent of WOS state | Yes | Daily at 14:00 UTC | Kept running |
| **async-deep-work** | No — daily deep work session | Yes | Daily at 05:00 UTC | Kept running |
| **upstream-sync** | No — syncs lobster repo from upstream | Yes | Daily at 08:00, 20:00 UTC | Kept running |
| **negentropic-sweep** | Borderline — reads WOS sweep context but independently valuable | Yes | Daily at 02:00 UTC | Kept running |
| **epistemic-drift-sweep** | No — evaluates Lobster epistemic behavior across sessions | Yes | Sun at 15:00 UTC | Kept running |
| **weekly-epistemic-retro** | No — weekly epistemic retrospective | Yes | Sun at 08:00 UTC | Kept running |
| **philosophy-discovery-scorer** | No — scores philosophy-explore sessions | Yes | Daily at 23:30 UTC | Kept running |
| **philosophy-harvest** | No — harvests philosophy session notes | Yes | Every 6 hours | Kept running |
| **philosophy-explore-weekly-synthesis** | No — synthesizes weekly philosophy notes | Yes | Sun at 08:00 UTC | Kept running |
| **lobster-hygiene** | No — general system hygiene | Yes | Every 3 days at 06:00 UTC | Kept running |
| **lobster-hygiene-biweekly** | No — biweekly system hygiene | Yes | 1st, 15th at 10:00 UTC | Kept running |
| **weekly-hygiene** | No — weekly hygiene job | Yes | Mon at 03:00 UTC | Kept running |
| **granola-ingest** | No — ingests Granola meeting notes | Yes | Every 15 min | Kept running |
| **usage-alert** | No — Claude Code usage threshold alerting | Yes | Hourly | Kept running |
| **pending-actions-nudge** | No — nudges for pending action items | Yes | (cron-direct) | Kept running |
| **obsidian-vault-sync** | No — syncs Obsidian vault | Yes | Every 15 min | Kept running |
| **dead-letter-sweep** | No — sweeps dead-letter queue | Yes | Mon at 09:00 UTC | Kept running |
| **system-retrospective** | No — system retrospective | Yes | Sun at 06:00 UTC | Kept running |
| **structural-hygiene-audit** | No — annual structural audit | Yes | Mar 31 annually | Kept running (rarely fires) |
| **garden-caretaker** | No — garden task caretaker | Yes | Every 15 min | Kept running |
| **quick-classifier-loop** | WOS-adjacent — was already disabled | No | Every 5 min | Already disabled — no action |
| **slow-reclassifier-loop** | WOS-adjacent — was already disabled | No | Every 15 min | Already disabled — no action |
| **phase2-design-review** | WOS-adjacent — was already disabled | No | Every 30 min | Already disabled — no action |
| **philosophy-explore-1** | No — was already disabled | No | Every 4 hours | Already disabled — no action |
| **surface-queue-delivery** | No — was already disabled | No | Daily at 08:00 | Already disabled — no action |

---

## Summary of Actions Taken

**Disabled (14 jobs):**
1. `executor-heartbeat` — core WOS dispatch loop, every 3 min
2. `steward-heartbeat` — core WOS steward pass, every 3 min
3. `issue-sweeper` — nightly GitHub sweep feeding UoW registry
4. `uow-reflection` — overnight WOS pipeline summary
5. `pattern-candidate-sweep` — meta-thread candidate surfacing
6. `github-issue-cultivator` — GitHub issue cultivation for WOS intake
7. `proposals-authorship` — weekly oracle-learnings-to-proposals authorship
8. `wos-overnight-loop` — overnight WOS test cycle observer
9. `wos-hourly-observation` — hourly WOS pipeline observation
10. `wos-queue-monitor` — backlog depth governor
11. `wos-health-check` — UoW starvation/heartbeat health check
12. `wos-metabolic-digest` — daily UoW metabolic classification digest
13. `wos-pr-sweeper` — WOS PR staleness scanner
14. `wos-health-monitor` — 5-min pipeline heartbeat monitor

**Kept running (all non-WOS infra):** morning-briefing, async-deep-work, upstream-sync, negentropic-sweep, epistemic-drift-sweep, weekly-epistemic-retro, philosophy-*, lobster-hygiene, lobster-hygiene-biweekly, weekly-hygiene, granola-ingest, usage-alert, pending-actions-nudge, obsidian-vault-sync, dead-letter-sweep, system-retrospective, structural-hygiene-audit, garden-caretaker.

---

## Root Cause: The Spigot Problem

`wos stop` sets `execution_enabled=false` in `wos-config.json`. This only gates the **executor-heartbeat** (via `_is_job_enabled()` check). All other WOS-core jobs have no awareness of `execution_enabled`.

Result: when WOS is paused, 13 other WOS-core jobs keep running, burning tokens on work that has no effect:
- issue-sweeper proposes UoWs that no one will execute
- uow-reflection summarizes a pipeline producing nothing
- wos-health-check fires alerts about a pipeline that is intentionally stopped
- wos-metabolic-digest sends digests on a population that is not growing

---

## Recommendation: Atomic Spigot Control

### Option A — jobs.json tag-based (simplest, no new infra)

Add a `"wos_core": true` field to each WOS-core job entry in `jobs.json`. The `wos stop` command handler in `dispatcher_handlers.py` iterates jobs.json and calls `update_scheduled_job(enabled=False)` for every job with `wos_core: true`. Symmetrically, `wos start` re-enables them.

**Pros:** No new mechanism. Works with both cron-direct and llm-dispatch job types. The `wos_core` tag is self-documenting in the registry.

**Cons:** `update_scheduled_job` MCP tool fails for cron-direct jobs not in systemd. Those need direct jobs.json writes. Needs a helper function that handles both types.

### Option B — Centralized `wos-spigot.json` manifest

Create `~/lobster-workspace/data/wos-spigot.json` listing all WOS-core job names. The `wos stop`/`wos start` handler reads this manifest and disables/enables all listed jobs atomically.

**Pros:** Authoritative list lives in one place. Adding a new WOS-core job is one line in the manifest. Easier to audit.

**Cons:** Two files to keep in sync (manifest + jobs.json).

### Option C — `execution_enabled` gate in every WOS-core script (most robust)

Add `_is_wos_enabled()` check at the top of each WOS-core script/task that reads `wos-config.json`. If `execution_enabled=false`, the job exits immediately (no-op run). This makes individual jobs self-gating.

**Pros:** No centralized toggle needed. Each job is independently safe.
**Cons:** Changes needed in many places. LLM-dispatch task files need a preamble check. More surface area.

### Recommended: Option A + a small helper

Implement Option A with a `toggle_wos_core_jobs(enabled: bool)` helper in `dispatcher_handlers.py`:

```python
def toggle_wos_core_jobs(enabled: bool):
    """Enable or disable all WOS-core jobs atomically."""
    WOS_CORE_JOBS = [
        "executor-heartbeat", "steward-heartbeat", "issue-sweeper",
        "uow-reflection", "pattern-candidate-sweep", "github-issue-cultivator",
        "proposals-authorship", "wos-overnight-loop", "wos-hourly-observation",
        "wos-queue-monitor", "wos-health-check", "wos-metabolic-digest",
        "wos-pr-sweeper", "wos-health-monitor",
    ]
    # Update jobs.json directly for cron-direct jobs
    # Call update_scheduled_job MCP tool for systemd-managed jobs
    ...
```

Called by both `wos start` and `wos stop` handlers. This is a one-line addition to each handler.

---

## For `wos start` Re-Enablement

To re-enable the right jobs when WOS restarts, the handler should:

1. Set `execution_enabled=true` in `wos-config.json`
2. Call `toggle_wos_core_jobs(enabled=True)` — re-enables all 14 jobs listed above
3. Send Dan a confirmation: "WOS started. N jobs re-enabled: [list]"

Note: `wos-overnight-loop` was a test harness (per its task file) and may not need re-enabling permanently. Suggest leaving it disabled by default and re-enabling manually if a new test run is needed.

---

*Audit performed: 2026-05-07. Author: wos-spigot-audit subagent.*
