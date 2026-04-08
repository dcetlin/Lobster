#!/usr/bin/env python3
"""
WOS Queue Monitor — Asymmetric governor: backlog starvation and toxicity observation.

Runs every 30 minutes. On each invocation:
1. Queries uow_registry for count of UoWs with status in
   ('ready-for-steward', 'ready-for-executor', 'active').
2. Appends {"timestamp": "<ISO>", "queue_depth": N} to
   ~/lobster-workspace/data/queue-depth-history.jsonl.
3. Reads the rolling history to detect:
   - STARVATION: queue depth 0 for >= 6 consecutive hours
     (12+ consecutive 30-min readings at depth 0).
   - TOXICITY: queue depth > 10 for >= 3 consecutive readings.
4. Emits write_task_output observations when either condition is met.
5. Checks jobs.json for its own `enabled` gate before running.

This is a pure observation instrument. No automated action is taken.

Cron schedule (every 30 minutes):
    */30 * * * * cd ~/lobster && uv run scheduled-tasks/wos-queue-monitor.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-queue-monitor.log 2>&1 # LOBSTER-WOS-QUEUE-MONITOR

Type C dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-queue-monitor.py

Design reference: docs/mito-modeling.md Section 2, Row 4; Section 5.2
GitHub issue: https://github.com/dcetlin/Lobster/issues/681
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
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
log = logging.getLogger("wos-queue-monitor")

# ---------------------------------------------------------------------------
# Constants — derived from the spec in issue #681 and mito-modeling.md
# ---------------------------------------------------------------------------

JOB_NAME: str = "wos-queue-monitor"

# Queue depth 0 for this many consecutive readings (30-min cadence) = 6 hours
STARVATION_CONSECUTIVE_READINGS: int = 12

# Queue depth > this threshold for TOXICITY_CONSECUTIVE_READINGS = toxicity signal
TOXICITY_DEPTH_THRESHOLD: int = 10
TOXICITY_CONSECUTIVE_READINGS: int = 3

# UoW statuses that contribute to the "active backlog" depth
BACKLOG_STATUSES: tuple[str, ...] = (
    "ready-for-steward",
    "ready-for-executor",
    "active",
)


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


def _history_file() -> Path:
    return _workspace() / "data" / "queue-depth-history.jsonl"


def _task_outputs_dir() -> Path:
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "task-outputs"


def _jobs_file() -> Path:
    return _workspace() / "scheduled-jobs" / "jobs.json"


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
# Registry query — pure function, returns int
# ---------------------------------------------------------------------------

def query_queue_depth(db_path: Path) -> int:
    """
    Query uow_registry for count of UoWs with status in BACKLOG_STATUSES.

    Returns 0 if the DB does not exist or the table is absent (safe default
    during first-run before the registry is initialized).
    """
    if not db_path.exists():
        log.warning("Registry DB not found at %s — reporting depth 0", db_path)
        return 0

    placeholders = ",".join("?" * len(BACKLOG_STATUSES))
    sql = f"SELECT COUNT(*) FROM uow_registry WHERE status IN ({placeholders})"
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            row = conn.execute(sql, BACKLOG_STATUSES).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Failed to query registry at %s: %s — reporting depth 0", db_path, exc)
        return 0


# ---------------------------------------------------------------------------
# History file I/O — pure reads, isolated append
# ---------------------------------------------------------------------------

def _load_history(history_file: Path) -> list[dict]:
    """Load all entries from the JSONL history file. Returns [] if absent or empty."""
    if not history_file.exists():
        return []
    entries = []
    for line in history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed history line: %r", line)
    return entries


def _append_history(history_file: Path, timestamp: str, queue_depth: int) -> None:
    """Append a new reading to the JSONL history file (side effect isolated here)."""
    history_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": timestamp, "queue_depth": queue_depth}
    with history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Signal detection — pure functions operating on history list
# ---------------------------------------------------------------------------

def detect_starvation(history: list[dict]) -> bool:
    """
    Return True if the last STARVATION_CONSECUTIVE_READINGS entries all have
    queue_depth == 0.

    At 30-minute cadence, STARVATION_CONSECUTIVE_READINGS readings = 6 hours.
    """
    if len(history) < STARVATION_CONSECUTIVE_READINGS:
        return False
    tail = history[-STARVATION_CONSECUTIVE_READINGS:]
    return all(entry.get("queue_depth", -1) == 0 for entry in tail)


def detect_toxicity(history: list[dict]) -> tuple[bool, int]:
    """
    Return (True, current_depth) if the last TOXICITY_CONSECUTIVE_READINGS
    entries all have queue_depth > TOXICITY_DEPTH_THRESHOLD, else (False, 0).
    """
    if len(history) < TOXICITY_CONSECUTIVE_READINGS:
        return False, 0
    tail = history[-TOXICITY_CONSECUTIVE_READINGS:]
    depths = [entry.get("queue_depth", 0) for entry in tail]
    if all(d > TOXICITY_DEPTH_THRESHOLD for d in depths):
        return True, depths[-1]
    return False, 0


# ---------------------------------------------------------------------------
# Task output writer — side effect isolated here
# ---------------------------------------------------------------------------

def _write_task_output(output: str, status: str, timestamp: str) -> None:
    """
    Write a task output record to the task-outputs directory.
    Mirrors the format expected by check_task_outputs / the MCP tool.
    """
    task_outputs = _task_outputs_dir()
    task_outputs.mkdir(parents=True, exist_ok=True)
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-{JOB_NAME}.json"
    record = {
        "job_name": JOB_NAME,
        "timestamp": timestamp,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    log.info("Wrote task output: %s → %s", status, output)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not _is_job_enabled():
        log.info("Job %s is disabled in jobs.json — exiting.", JOB_NAME)
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    db_path = _registry_db_path()
    history_file = _history_file()

    # 1. Query current queue depth
    depth = query_queue_depth(db_path)
    log.info("Queue depth: %d (statuses: %s)", depth, ", ".join(BACKLOG_STATUSES))

    # 2. Append to history
    _append_history(history_file, timestamp, depth)

    # 3. Load history and run signal detection
    history = _load_history(history_file)

    starvation = detect_starvation(history)
    toxicity, toxic_depth = detect_toxicity(history)

    # 4. Emit observations when signals fire
    if starvation:
        msg = (
            f"STARVATION: queue depth 0 for {STARVATION_CONSECUTIVE_READINGS}+ "
            f"consecutive readings ({STARVATION_CONSECUTIVE_READINGS * 30} minutes)"
        )
        log.warning(msg)
        _write_task_output(msg, "success", timestamp)

    if toxicity:
        msg = (
            f"TOXICITY: queue depth >{TOXICITY_DEPTH_THRESHOLD} for "
            f"{TOXICITY_CONSECUTIVE_READINGS}+ consecutive readings, "
            f"current depth: {toxic_depth}"
        )
        log.warning(msg)
        _write_task_output(msg, "success", timestamp)

    if not starvation and not toxicity:
        log.info("No anomalies detected. Queue depth: %d", depth)


if __name__ == "__main__":
    main()
