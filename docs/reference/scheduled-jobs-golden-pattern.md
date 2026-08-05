# Scheduled Jobs — Golden Canonical Pattern

Reference for how LLM scheduled jobs must be set up in Lobster. When adding a new recurring LLM task, follow this pattern exactly.

**Related docs:** [CLAUDE.md](../CLAUDE.md) — scheduling architecture and job type distinction | [upgrade.sh](../scripts/upgrade.sh) — Migration 87 for automated install

---

## The Four Required Components

Every LLM scheduled job (Type A) requires exactly four artifacts:

### 1. systemd `.timer` file

Location: `~/lobster/services/lobster-{job-name}.timer`

```ini
[Unit]
Description={Human-readable description}
# LOBSTER-MANAGED

[Timer]
OnCalendar={systemd calendar expression}
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` ensures the job fires on next boot if it was missed (e.g., system was down at scheduled time).

### 2. systemd `.service` file

Location: `~/lobster/services/lobster-{job-name}.service`

```ini
[Unit]
Description={Human-readable description}
# LOBSTER-MANAGED

[Service]
Type=oneshot
User=lobster
ExecStart=/home/lobster/lobster/scheduled-tasks/dispatch-job.sh {job-name}
```

`Type=oneshot` is correct for LLM jobs — the service exits after the job dispatch completes.

### 3. `jobs.json` entry

Location: `~/lobster-workspace/scheduled-jobs/jobs.json`

Managed via the `create_scheduled_job` MCP tool. The `enabled` field in jobs.json is the runtime gate — set it to `false` to pause a job without touching systemd.

```json
{
  "name": "{job-name}",
  "schedule": "{human-readable schedule}",
  "enabled": true,
  "dispatch": "systemd"
}
```

### 4. Task definition `.md` file

Location: `~/lobster-workspace/scheduled-jobs/tasks/{job-name}.md`

Contains the prompt context dispatched to the LLM subagent. Managed via `create_scheduled_job` / `update_scheduled_job` MCP tools.

---

## Install and Enable

After adding unit files to `services/`, install them:

```bash
sudo cp ~/lobster/services/lobster-{job-name}.timer /etc/systemd/system/
sudo cp ~/lobster/services/lobster-{job-name}.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lobster-{job-name}.timer
```

Verify with:

```bash
systemctl list-timers --all | grep lobster-{job-name}
```

---

## Type C Exception (cron-direct scripts)

Type C jobs (pure Python scripts with no LLM round-trip) are correctly run via cron — **not** via systemd timers. These are identified by `"dispatch": "cron-direct"` in jobs.json. Examples: `executor-heartbeat.py`, `steward-heartbeat.py`, `wos-queue-monitor.py`.

**Rule: never use cron for Type A (LLM) jobs.** Cron bypasses the `list_scheduled_jobs` MCP validation and `jobs.json` enable/disable gate. Use systemd timers for all LLM-dispatched work.

---

## Naming Convention

| Artifact | Name pattern |
|---|---|
| systemd timer | `lobster-{job-name}.timer` |
| systemd service | `lobster-{job-name}.service` |
| jobs.json key | `{job-name}` |
| task file | `{job-name}.md` |
| dispatch-job.sh arg | `{job-name}` |

Where `{job-name}` is lowercase-hyphenated (e.g., `morning-briefing`, `pattern-candidate-sweep`).

---

## Jobs Migrated from Cron (issue #869)

These 10 jobs were converted from cron `LOBSTER-SCHEDULED` entries to systemd timers. Their unit files live in `services/` and are installed by Migration 87 in `upgrade.sh`.

| Job | Timer schedule |
|---|---|
| `weekly-epistemic-retro` | Sun 08:00 UTC |
| `lobster-hygiene` | Every 3 days (1,4,7,… 06:00 UTC) |
| `pattern-candidate-sweep` | Wed 08:00 UTC |
| `morning-briefing` | Daily 14:00 UTC |
| `uow-reflection` | Daily 06:30 UTC |
| `structural-hygiene-audit` | Mar 31 12:00 UTC (annual) |
| `upstream-sync` | Daily 08:00 and 20:00 UTC |
| `lobster-hygiene-biweekly` | 1st and 15th 10:00 UTC |
| `github-issue-cultivator` | Daily 06:00 UTC |
| `wos-hourly-observation` | 06–12 UTC hourly |
