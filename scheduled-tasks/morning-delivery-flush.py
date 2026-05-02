#!/usr/bin/env python3
"""
Morning Delivery Flush — Lobster Scheduled Job
===============================================

Delivers all messages that were queued by the circadian delivery gate during
off-peak hours. Runs once daily at 14:00 UTC (06:00 PST / 07:00 PDT).

If the queue is empty, logs silently and exits without sending a Telegram
notification (no noise when there is nothing to deliver).

Type C dispatch: cron calls this script directly. No LLM round-trip needed —
queue drain is mechanical.

Cron entry (added by upgrade.sh migration 93):
    0 14 * * * cd ~/lobster && uv run scheduled-tasks/morning-delivery-flush.py >> ~/lobster-workspace/scheduled-jobs/logs/morning-delivery-flush.log 2>&1 # LOBSTER-MORNING-DELIVERY-FLUSH

Run standalone:
    uv run ~/lobster/scheduled-tasks/morning-delivery-flush.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import _inbox_dir, _task_outputs_dir  # noqa: E402
from src.delivery.circadian import flush_morning_queue  # noqa: E402

JOB_NAME = "morning-delivery-flush"


def _make_send_fn(dry_run: bool):
    """Return a send_fn suitable for flush_morning_queue."""

    def send_fn(chat_id: int, text: str) -> None:
        if dry_run:
            print(f"  [dry-run] would send to chat_id={chat_id}: {text[:80]}...")
            return
        inbox = _inbox_dir()
        msg_id = f"{JOB_NAME}_{uuid.uuid4().hex}"
        source = os.environ.get("LOBSTER_DEFAULT_SOURCE", "telegram")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = {
            "id": msg_id,
            "type": "subagent_result",
            "task_id": msg_id,
            "chat_id": chat_id,
            "source": source,
            "text": text,
            "status": "success",
            "sent_reply_to_user": False,
            "timestamp": timestamp,
        }
        out_path = inbox / f"{msg_id}.json"
        tmp_path = Path(str(out_path) + ".tmp")
        tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(out_path)

    return send_fn


def _write_task_output(output: str, status: str, timestamp: str) -> None:
    task_outputs = _task_outputs_dir()
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


def run(dry_run: bool = False) -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] Starting {JOB_NAME}")

    send_fn = _make_send_fn(dry_run)

    try:
        count = flush_morning_queue(send_fn)
    except Exception as exc:
        msg = f"flush_morning_queue failed: {exc}"
        print(f"  ERROR: {msg}", file=sys.stderr)
        _write_task_output(msg, "error", timestamp)
        return 1

    if count == 0:
        print(f"[{timestamp}] Queue empty — nothing to deliver")
        _write_task_output("Queue empty — nothing to deliver.", "success", timestamp)
    else:
        summary = f"Delivered {count} queued message(s) from pending-deliveries.jsonl."
        print(f"[{timestamp}] {summary}")
        _write_task_output(summary, "success", timestamp)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent without delivering")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))
