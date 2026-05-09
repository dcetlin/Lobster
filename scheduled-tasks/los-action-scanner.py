#!/usr/bin/env python3
"""
LOS Action Item Scanner — Lobster Scheduled Job

Runs hourly. Scans the past hour of conversation history for action
commitments Dan has made, extracts them, and writes them to self_action_items.db.

This is a Type A job (LLM subagent): the subagent reads recent messages,
uses its native intelligence to extract action commitments from each message,
then calls parse_llm_response() + extract_action_items() to persist them to the DB.

The extraction step is performed by the subagent (Claude natively) — this
script handles message collection only, emitting candidates as JSON to stdout.
The subagent formats extracted items as JSON and calls parse_llm_response() then
extract_action_items() from src.los.extractor before persisting to the DB.

Type B (cron-direct) was considered but rejected: the extraction step
requires LLM reasoning — making it unsuitable for sub-15-minute cron runs.
Hourly is appropriate.

jobs.json entry:
    {
        "name": "los-action-scanner",
        "schedule": "0 * * * *",
        "task_file": "tasks/los-action-scanner.md",
        "enabled": true
    }

Cron entry (alternative for Type B-style direct invocation):
    0 * * * * cd ~/lobster && uv run scheduled-tasks/los-action-scanner.py >> ~/lobster-workspace/scheduled-jobs/logs/los-action-scanner.log 2>&1 # LOBSTER-LOS-ACTION-SCANNER

Run standalone (for testing):
    uv run ~/lobster/scheduled-tasks/los-action-scanner.py [--dry-run] [--hours N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import _task_outputs_dir  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("los-action-scanner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "los-action-scanner"
DEFAULT_LOOKBACK_HOURS = 1
ADMIN_CHAT_ID = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")
MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))


# ---------------------------------------------------------------------------
# Jobs.json enabled gate — Type A dispatch compliance
# ---------------------------------------------------------------------------


def _is_job_enabled(job_name: str) -> bool:
    """Return True if the job is enabled in jobs.json."""
    try:
        jobs_file = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")) / "scheduled-jobs" / "jobs.json"
        with jobs_file.open() as fh:
            data = json.load(fh)
        jobs = data.get("jobs", {})
        entry = jobs.get(job_name, {})
        return bool(entry.get("enabled", True))
    except Exception:
        return True  # Default to enabled when unreadable


# ---------------------------------------------------------------------------
# Message reading
# ---------------------------------------------------------------------------


def _load_processed_messages(since: datetime) -> list[dict]:
    """Load messages from ~/messages/processed/ newer than `since`."""
    processed_dir = MESSAGES_DIR / "processed"
    if not processed_dir.exists():
        return []

    messages = []
    for msg_file in processed_dir.glob("*.json"):
        try:
            with msg_file.open() as fh:
                msg = json.load(fh)
            # Filter: only user messages (not subagent results), within window
            ts_str = msg.get("timestamp") or msg.get("created_at")
            if not ts_str:
                continue
            # Parse timestamp — may be in various formats
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < since:
                continue
            # Only scan actual user messages
            msg_type = msg.get("type", "")
            if msg_type in ("subagent_result", "subagent_notification", "wos_execute"):
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue
            messages.append(msg)
        except Exception:
            continue

    return messages


def _write_task_output(output: str, status: str = "success") -> None:
    """Write output to the task outputs directory."""
    try:
        outputs_dir = _task_outputs_dir()
        outputs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_file = outputs_dir / f"{JOB_NAME}-{ts}.json"
        with out_file.open("w") as fh:
            json.dump({"job_name": JOB_NAME, "output": output, "status": status, "at": ts}, fh)
    except Exception as exc:
        log.warning("Failed to write task output: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="LOS action item scanner")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_HOURS, help="Lookback window in hours")
    args = parser.parse_args()

    if not _is_job_enabled(JOB_NAME):
        log.info("Job %s is disabled in jobs.json — exiting.", JOB_NAME)
        return

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    log.info("Scanning messages since %s (lookback=%dh, dry_run=%s)", since.isoformat(), args.hours, args.dry_run)

    messages = _load_processed_messages(since)
    log.info("Found %d candidate messages to scan.", len(messages))

    if not messages:
        _write_task_output("No messages in lookback window.", status="success")
        return

    # Emit the candidate messages as JSON for the subagent to process.
    # The subagent reads each message, uses its native intelligence to extract
    # action commitments, then calls parse_llm_response() + extract_action_items()
    # from src.los.extractor to persist them to the DB.
    candidate_texts = [
        {
            "msg_id": msg.get("id") or msg.get("message_id", ""),
            "text": msg.get("text", "").strip(),
        }
        for msg in messages
        if len(msg.get("text", "").strip()) >= 10
    ]
    log.info(
        "Emitting %d candidate message(s) for subagent extraction.",
        len(candidate_texts),
    )
    import json as _json
    print(_json.dumps({"candidates": candidate_texts}, indent=2))
    _write_task_output(
        f"Collected {len(candidate_texts)} candidate messages for subagent extraction.",
        status="success",
    )


if __name__ == "__main__":
    main()
