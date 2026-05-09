#!/usr/bin/env python3
"""
LOS Action Item Scanner — Lobster Scheduled Job

Runs hourly. Scans the past hour of conversation history for action
commitments Dan has made, extracts them via the LLM extractor, and
writes them to self_action_items.db.

This is a Type A job (LLM subagent): the MCP conversation history tool
requires the context of a running Lobster session — the scanner reads
recent messages via the inbox_server's conversation history, then calls
Claude to extract commitments.

Type B (cron-direct) was considered but rejected: the extraction step
requires the Anthropic SDK (LLM call), which carries cost and latency —
making it unsuitable for sub-15-minute cron runs. Hourly is appropriate.

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
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import connect, get_open_items, DEFAULT_DB_PATH  # noqa: E402
from src.los.extractor import extract_action_items  # noqa: E402
from src.utils.inbox_write import _inbox_dir, _task_outputs_dir  # noqa: E402

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


def _scan_and_extract(
    conn,
    messages: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """Run extraction on each message. Returns list of extraction result summaries."""
    results = []
    for msg in messages:
        text = msg.get("text", "").strip()
        if not text or len(text) < 10:
            continue
        source_id = msg.get("id") or msg.get("message_id", "")
        source = "telegram"

        if dry_run:
            log.info("[dry-run] Would extract from: %s...", text[:80])
            results.append({"msg_id": source_id, "extracted": [], "dry_run": True})
            continue

        try:
            extracted = extract_action_items(
                conn=conn,
                text=text,
                source=source,
                source_message_id=str(source_id),
            )
            if extracted:
                log.info(
                    "Extracted %d action item(s) from msg %s: %s",
                    len(extracted),
                    source_id,
                    [i.text[:60] for i in extracted],
                )
            results.append({
                "msg_id": source_id,
                "extracted": [{"id": i.id, "text": i.text} for i in extracted],
            })
        except Exception as exc:
            log.warning("Extraction failed for msg %s: %s", source_id, exc)
            results.append({"msg_id": source_id, "extracted": [], "error": str(exc)})

    return results


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

    if args.dry_run:
        conn = None
    else:
        conn = connect(DEFAULT_DB_PATH)

    try:
        results = _scan_and_extract(conn, messages, dry_run=args.dry_run)
    finally:
        if conn:
            conn.close()

    total_extracted = sum(len(r.get("extracted", [])) for r in results)
    summary = f"Scanned {len(messages)} messages, extracted {total_extracted} action items."
    log.info(summary)
    _write_task_output(summary, status="success")


if __name__ == "__main__":
    main()
