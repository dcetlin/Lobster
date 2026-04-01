#!/usr/bin/env python3
"""
RALPH Loop — Recursive Autonomous Loop for Pipeline Health.

Runs every 3 hours as a Type A (LLM subagent) scheduled job.
On each invocation:
1. Checks the jobs.json enabled gate.
2. Writes a scheduled_job_trigger inbox message that causes the dispatcher to
   spawn a subagent with the ralph-loop.md task definition.

The actual multi-step loop logic (inject → execute → observe → report → fix → track)
lives in the task definition at scheduled-jobs/tasks/ralph-loop.md.
The subagent that runs that task has access to all MCP tools and the shell.

Cron schedule (every 3 hours):
    0 */3 * * * cd ~/lobster && uv run scheduled-tasks/ralph-loop.py >> \
        ~/lobster-workspace/scheduled-jobs/logs/ralph-loop.log 2>&1

Run standalone:
    uv run ~/lobster/scheduled-tasks/ralph-loop.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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
log = logging.getLogger("ralph-loop")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "ralph-loop"
WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
JOBS_FILE = WORKSPACE / "scheduled-jobs" / "jobs.json"
TASK_FILE = WORKSPACE / "scheduled-jobs" / "tasks" / "ralph-loop.md"
INBOX_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")) / "inbox"
RALPH_STATE_FILE = WORKSPACE / "data" / "ralph-state.json"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _is_job_enabled() -> bool:
    """Return True if ralph-loop is enabled in jobs.json.

    Defaults to True when jobs.json is absent, the entry is missing, or the
    file is malformed — mirrors the gate logic in dispatch-job.sh.
    """
    try:
        data = json.loads(JOBS_FILE.read_text())
        return bool(data.get("jobs", {}).get(JOB_NAME, {}).get("enabled", True))
    except Exception:
        return True


def _load_ralph_state() -> dict:
    """Return current RALPH state dict, initializing defaults if absent."""
    defaults: dict = {
        "consecutive_clean_runs": 0,
        "total_runs": 0,
        "last_run_ts": None,
        "last_anomalies": [],
    }
    try:
        raw = RALPH_STATE_FILE.read_text()
        state = json.loads(raw)
        # Merge with defaults to handle new keys added in later versions
        return {**defaults, **state}
    except Exception:
        return defaults


def _task_definition() -> str:
    """Return the task definition markdown content."""
    try:
        return TASK_FILE.read_text()
    except Exception as e:
        log.warning("Could not read task file %s: %s", TASK_FILE, e)
        return f"# RALPH Loop\n\nTask file not found at {TASK_FILE}."


def _write_inbox_message(payload: dict, dry_run: bool) -> Path | None:
    """Write a JSON message to the inbox directory.

    Returns the path written, or None on dry_run / error.
    Pure effect boundary: all file I/O is isolated here.
    """
    if dry_run:
        log.info("DRY RUN: would write inbox message: %s", json.dumps(payload, indent=2))
        return None

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    import time
    filename = f"ralph-loop-{int(time.time())}.json"
    dest = INBOX_DIR / filename
    dest.write_text(json.dumps(payload, indent=2))
    log.info("Wrote inbox message: %s", dest)
    return dest


def _build_inbox_message() -> dict:
    """Build the scheduled_job_trigger inbox payload for the dispatcher."""
    return {
        "type": "scheduled_job_trigger",
        "job_name": JOB_NAME,
        "task": _task_definition(),
        "source": "cron",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="RALPH Loop — Pipeline Health Scheduler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check state and log intent without writing to inbox",
    )
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    log.info("RALPH loop trigger starting%s", " (DRY RUN)" if dry_run else "")

    if not _is_job_enabled():
        log.info("RALPH loop: skipped (disabled in jobs.json)")
        return 0

    state = _load_ralph_state()
    log.info(
        "RALPH state: consecutive_clean=%d total_runs=%d last_run=%s",
        state["consecutive_clean_runs"],
        state["total_runs"],
        state.get("last_run_ts") or "never",
    )

    payload = _build_inbox_message()
    written = _write_inbox_message(payload, dry_run=dry_run)

    if not dry_run and written:
        log.info("RALPH loop trigger dispatched — subagent will run the full pipeline loop")
    elif dry_run:
        log.info("RALPH loop trigger: dry run complete")

    return 0


if __name__ == "__main__":
    sys.exit(main())
