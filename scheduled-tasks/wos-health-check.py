#!/usr/bin/env python3
"""
WOS Health Check — starvation diagnosis and heartbeat liveness observation.

Runs every 6 hours. On each invocation:
1. Checks for UoWs stuck in 'proposed' or 'pending' state for >24h (starvation candidates).
2. Checks for UoWs in 'active'/'executing' with stale heartbeats (heartbeat liveness check).
3. Checks executor-heartbeat last-run time (is the heartbeat even firing?).
4. Writes a brief health report to the health-check log.
5. If any UoWs are stale (stuck >48h), sends an inbox message to the admin chat_id.

Cron schedule (every 6 hours):
    0 */6 * * * cd ~/lobster && uv run scheduled-tasks/wos-health-check.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-health-check.log 2>&1 # LOBSTER-WOS-HEALTH-CHECK

Type C dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-health-check.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-health-check")

# ---------------------------------------------------------------------------
# Constants — derived from the spec in the audit doc and issue #849
# ---------------------------------------------------------------------------

JOB_NAME: str = "wos-health-check"

# UoWs stuck in proposed/pending for this long are starvation candidates
STARVATION_THRESHOLD_HOURS: int = 24

# UoWs stuck >48h in any in-flight state trigger an inbox alert
ALERT_THRESHOLD_HOURS: int = 48

# Heartbeat stale threshold: silence beyond heartbeat_ttl + this buffer = stall
HEARTBEAT_STALE_BUFFER_SECONDS: int = 60

# Admin chat_id for inbox alerts (env-injected, falls back to hardcoded)
ADMIN_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

# Statuses considered starvation candidates (not yet in-flight)
STARVATION_STATUSES: tuple[str, ...] = ("proposed", "pending")

# Statuses considered in-flight (executing)
IN_FLIGHT_STATUSES: tuple[str, ...] = ("active", "executing")


# ---------------------------------------------------------------------------
# Workspace / path helpers
# ---------------------------------------------------------------------------

def _workspace() -> Path:
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


def _registry_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return _workspace() / "orchestration" / "registry.db"


def _log_file() -> Path:
    return _workspace() / "scheduled-jobs" / "logs" / "wos-health-check.log"


def _jobs_file() -> Path:
    return _workspace() / "scheduled-jobs" / "jobs.json"


def _inbox_dir() -> Path:
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "inbox"


def _executor_log_file() -> Path:
    return _workspace() / "scheduled-jobs" / "logs" / "executor-heartbeat.log"


# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type C dispatch pattern
# ---------------------------------------------------------------------------

def _is_job_enabled() -> bool:
    """
    Return True if this job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when:
    - jobs.json is absent
    - the job entry is missing
    - the file is unreadable or malformed
    """
    try:
        data = json.loads(_jobs_file().read_text())
        return bool(data.get("jobs", {}).get(JOB_NAME, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Registry queries — pure functions returning structured data
# ---------------------------------------------------------------------------

def query_starvation_candidates(db_path: Path, threshold_hours: int) -> list[dict]:
    """
    Return UoWs stuck in 'proposed' or 'pending' for longer than threshold_hours.

    Each result dict contains: id, status, summary, created_at, age_hours.
    Returns [] if DB absent or table missing.
    """
    if not db_path.exists():
        log.warning("Registry DB not found at %s", db_path)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    placeholders = ",".join("?" * len(STARVATION_STATUSES))
    sql = f"""
        SELECT id, status, summary, created_at
        FROM uow_registry
        WHERE status IN ({placeholders})
          AND created_at <= ?
        ORDER BY created_at ASC
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, (*STARVATION_STATUSES, cutoff)).fetchall()
            now = datetime.now(timezone.utc)
            results = []
            for row in rows:
                created = row["created_at"] or ""
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_hours = round((now - created_dt).total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    age_hours = None
                results.append({
                    "id": row["id"],
                    "status": row["status"],
                    "summary": row["summary"] or "(no summary)",
                    "created_at": created,
                    "age_hours": age_hours,
                })
            return results
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Starvation query failed: %s", exc)
        return []


def query_stale_heartbeats(db_path: Path, buffer_seconds: int) -> list[dict]:
    """
    Return UoWs in 'active'/'executing' whose heartbeat is stale.

    Stale = (now - heartbeat_at) > heartbeat_ttl + buffer_seconds,
            when heartbeat_at IS NOT NULL.

    UoWs with heartbeat_at = NULL use the legacy started_at path; they are
    reported separately as 'no_heartbeat' to distinguish from true stalls.

    Each result dict contains: id, status, heartbeat_at, heartbeat_ttl,
    started_at, staleness_seconds, stall_type.
    Returns [] if DB absent or table missing.
    """
    if not db_path.exists():
        return []

    placeholders = ",".join("?" * len(IN_FLIGHT_STATUSES))
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            rows = conn.execute(
                f"""
                SELECT id, status, heartbeat_at, heartbeat_ttl, started_at
                FROM uow_registry
                WHERE status IN ({placeholders})
                """,
                IN_FLIGHT_STATUSES,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Heartbeat liveness query failed: %s", exc)
        return []

    now = datetime.now(timezone.utc)
    stale = []
    for row in rows:
        heartbeat_at = row["heartbeat_at"]
        heartbeat_ttl = row["heartbeat_ttl"] or 300
        started_at = row["started_at"]

        if heartbeat_at is not None:
            try:
                hb_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
                staleness = (now - hb_dt).total_seconds()
                if staleness > heartbeat_ttl + buffer_seconds:
                    stale.append({
                        "id": row["id"],
                        "status": row["status"],
                        "heartbeat_at": heartbeat_at,
                        "heartbeat_ttl": heartbeat_ttl,
                        "started_at": started_at,
                        "staleness_seconds": round(staleness),
                        "stall_type": "heartbeat",
                    })
            except (ValueError, TypeError):
                pass
        else:
            # No heartbeat written yet — report as no_heartbeat (legacy path)
            if started_at:
                try:
                    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    age_seconds = (now - start_dt).total_seconds()
                    stale.append({
                        "id": row["id"],
                        "status": row["status"],
                        "heartbeat_at": None,
                        "heartbeat_ttl": heartbeat_ttl,
                        "started_at": started_at,
                        "staleness_seconds": round(age_seconds),
                        "stall_type": "no_heartbeat",
                    })
                except (ValueError, TypeError):
                    pass

    return stale


def query_long_running_in_flight(db_path: Path, threshold_hours: int) -> list[dict]:
    """
    Return UoWs in any in-flight status that have been running longer than
    threshold_hours (alert candidates for inbox notification).

    Returns each record with id, status, started_at, age_hours.
    """
    if not db_path.exists():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    placeholders = ",".join("?" * len(IN_FLIGHT_STATUSES))
    sql = f"""
        SELECT id, status, summary, started_at, created_at
        FROM uow_registry
        WHERE status IN ({placeholders})
          AND (
            (started_at IS NOT NULL AND started_at <= ?)
            OR (started_at IS NULL AND created_at <= ?)
          )
        ORDER BY created_at ASC
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, (*IN_FLIGHT_STATUSES, cutoff, cutoff)).fetchall()
            now = datetime.now(timezone.utc)
            results = []
            for row in rows:
                ref_ts = row["started_at"] or row["created_at"] or ""
                try:
                    ref_dt = datetime.fromisoformat(ref_ts.replace("Z", "+00:00"))
                    age_hours = round((now - ref_dt).total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    age_hours = None
                results.append({
                    "id": row["id"],
                    "status": row["status"],
                    "summary": row["summary"] or "(no summary)",
                    "started_at": row["started_at"],
                    "age_hours": age_hours,
                })
            return results
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Long-running query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Executor-heartbeat last-run check — checks log file mtime
# ---------------------------------------------------------------------------

def check_executor_heartbeat_liveness() -> dict:
    """
    Check when executor-heartbeat last ran by inspecting its log file mtime.

    Returns a dict with: last_run_iso, age_minutes, is_stale (>10 minutes).
    When the log file is absent, returns is_stale=True with last_run_iso=None.
    """
    log_path = _executor_log_file()
    if not log_path.exists():
        return {"last_run_iso": None, "age_minutes": None, "is_stale": True, "reason": "log file absent"}

    try:
        mtime = log_path.stat().st_mtime
        last_run = datetime.fromtimestamp(mtime, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age_minutes = round((now - last_run).total_seconds() / 60, 1)
        is_stale = age_minutes > 10  # executor runs every 3 minutes; >10 = missed multiple cycles
        return {
            "last_run_iso": last_run.isoformat(),
            "age_minutes": age_minutes,
            "is_stale": is_stale,
            "reason": f"log mtime {age_minutes} minutes ago",
        }
    except Exception as exc:
        return {"last_run_iso": None, "age_minutes": None, "is_stale": True, "reason": str(exc)}


# ---------------------------------------------------------------------------
# Inbox message writer — sends alert for stale UoWs (side effect isolated)
# ---------------------------------------------------------------------------

def _write_inbox_alert(uow_ids: list[str], summary: str, dry_run: bool = False) -> None:
    """
    Write a wos_health_alert inbox message to ~/messages/inbox/ if stale UoWs detected.

    This is a fire-and-forget write — no delivery confirmation. The dispatcher
    picks up the message on its next cycle.

    In dry_run mode: logs the message but does not write the file.
    """
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "wos_health_alert",
        "chat_id": ADMIN_CHAT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "stale_uow_ids": uow_ids,
    }

    if dry_run:
        log.info("DRY RUN: would write inbox alert — %s", summary)
        return

    try:
        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Wrote inbox alert %s — %s", msg_id, summary)
    except Exception as exc:
        log.warning("Failed to write inbox alert: %s", exc)


# ---------------------------------------------------------------------------
# Report writer — writes health report to log file
# ---------------------------------------------------------------------------

def write_health_report(report: dict, log_path: Path) -> None:
    """
    Append a structured health report to the wos-health-check log file.

    The log file is a JSONL file — one JSON object per run. Human-readable
    summary is also logged via the standard logger for the cron log.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Failed to write health report to %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# Main health check
# ---------------------------------------------------------------------------

def run_health_check(db_path: Path, dry_run: bool = False) -> dict:
    """
    Run the full health check and return a structured report dict.

    This is the main entry point. All reads are pure; side effects
    (log write, inbox alert) are isolated at the boundaries.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Starvation candidates (stuck in proposed/pending >24h)
    starvation_candidates = query_starvation_candidates(db_path, STARVATION_THRESHOLD_HOURS)
    log.info(
        "Starvation candidates (stuck >%dh): %d",
        STARVATION_THRESHOLD_HOURS,
        len(starvation_candidates),
    )
    for uow in starvation_candidates:
        log.info("  STARVATION: %s | %s | age=%.1fh", uow["id"], uow["status"], uow.get("age_hours") or 0)

    # 2. Heartbeat liveness (stale heartbeats in active/executing)
    stale_heartbeats = query_stale_heartbeats(db_path, HEARTBEAT_STALE_BUFFER_SECONDS)
    log.info("Stale heartbeats / no-heartbeat UoWs: %d", len(stale_heartbeats))
    for uow in stale_heartbeats:
        log.info(
            "  STALE: %s | %s | stall_type=%s | staleness=%ds",
            uow["id"], uow["status"], uow["stall_type"], uow.get("staleness_seconds") or 0,
        )

    # 3. Executor-heartbeat liveness
    executor_status = check_executor_heartbeat_liveness()
    if executor_status["is_stale"]:
        log.warning(
            "Executor heartbeat appears stale — %s",
            executor_status.get("reason", "unknown"),
        )
    else:
        log.info(
            "Executor heartbeat OK — last run %.1f minutes ago",
            executor_status.get("age_minutes") or 0,
        )

    # 4. Long-running alert check (stuck >48h in any in-flight status)
    long_running = query_long_running_in_flight(db_path, ALERT_THRESHOLD_HOURS)
    if long_running:
        stale_ids = [u["id"] for u in long_running]
        alert_lines = [
            f"WOS Health Alert: {len(long_running)} UoW(s) stuck >48h in in-flight status."
        ]
        for u in long_running:
            alert_lines.append(
                f"  {u['id']} | {u['status']} | age={u.get('age_hours', '?')}h | {u['summary'][:80]}"
            )
        alert_summary = "\n".join(alert_lines)
        log.warning("ALERT: %s", alert_summary)
        _write_inbox_alert(stale_ids, alert_summary, dry_run=dry_run)

    # Build report
    report = {
        "timestamp": now_iso,
        "starvation_candidates_count": len(starvation_candidates),
        "starvation_threshold_hours": STARVATION_THRESHOLD_HOURS,
        "stale_heartbeats_count": len(stale_heartbeats),
        "long_running_alert_count": len(long_running),
        "alert_threshold_hours": ALERT_THRESHOLD_HOURS,
        "executor_heartbeat": executor_status,
        "starvation_candidates": starvation_candidates,
        "stale_heartbeats": stale_heartbeats,
        "long_running_uows": long_running,
        "dry_run": dry_run,
    }

    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="WOS health check — starvation and heartbeat liveness")
    parser.add_argument("--dry-run", action="store_true", help="Run checks but do not send inbox alerts")
    args = parser.parse_args()

    if not _is_job_enabled():
        log.info("Job '%s' is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    db_path = _registry_db_path()
    log.info("Running WOS health check — registry: %s dry_run=%s", db_path, args.dry_run)

    report = run_health_check(db_path, dry_run=args.dry_run)

    log_path = _log_file()
    write_health_report(report, log_path)
    log.info(
        "Health check complete — starvation=%d stale_hb=%d long_running=%d",
        report["starvation_candidates_count"],
        report["stale_heartbeats_count"],
        report["long_running_alert_count"],
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
