#!/usr/bin/env python3
"""
Reflective Surface Queue Delivery — Lobster Scheduled Job
==========================================================

Reads meta/reflective-surface-queue.json, selects up to 3 undelivered items
by priority score, formats them as Telegram messages, delivers them, and marks
each as delivered with a delivered_at timestamp.

Priority scoring (descending importance):
- Source weight: premise-review > hygiene-review (oracle items if added later)
- Category: items with "Misaligned" verdict surface before "Questioned"
- Age: older undelivered items surface first, capped at 14 days (older items
  are archived without delivery — they have drifted beyond actionable horizon)

Delivers max 3 items per run to avoid flooding.

Run standalone:
    uv run ~/lobster/scheduled-tasks/surface-queue-delivery.py
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUEUE_PATH = Path.home() / "lobster-workspace" / "meta" / "reflective-surface-queue.json"
MAX_DELIVER_PER_RUN = 3
ARCHIVE_AGE_DAYS = 14

SOURCE_WEIGHT: dict[str, int] = {
    "meta/premise-review.md": 30,
    "meta/oracle/learnings.md": 20,
    "meta/hygiene-review.md": 10,
}

DEFAULT_SOURCE_WEIGHT = 5


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_queued_at(item: dict) -> datetime | None:
    """Parse the queued_at field into a timezone-aware datetime, or None on failure."""
    raw = item.get("queued_at", "")
    if not raw:
        return None
    try:
        # Handle both 'Z' suffix and '+00:00' offset
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def is_delivered(item: dict) -> bool:
    return bool(item.get("delivered", False))


def is_archived(item: dict) -> bool:
    return bool(item.get("archived", False))


def age_days(item: dict, reference: datetime) -> float:
    """Return item age in days from reference time. Returns 0.0 if queued_at is missing."""
    queued = parse_queued_at(item)
    if queued is None:
        return 0.0
    delta = reference - queued
    return max(0.0, delta.total_seconds() / 86400.0)


def priority_score(item: dict, reference: datetime) -> float:
    """
    Compute a priority score (higher = surface sooner).

    Factors:
    - Source weight (30/20/10/5 depending on source_file)
    - Verdict boost: +15 for Misaligned, +8 for Questioned (from surface_reason text)
    - Age contribution: +1 per day old (so older undelivered items float up)
    """
    source_file = item.get("source_file", "")
    weight = SOURCE_WEIGHT.get(source_file, DEFAULT_SOURCE_WEIGHT)

    # Scan surface_reason for alignment verdict keywords
    surface_reason = item.get("surface_reason", "").lower()
    verdict_boost = 0
    if "misaligned" in surface_reason:
        verdict_boost = 15
    elif "questioned" in surface_reason:
        verdict_boost = 8

    age = age_days(item, reference)

    return weight + verdict_boost + age


def select_items(items: list[dict], reference: datetime) -> tuple[list[dict], list[dict]]:
    """
    Partition items into (to_deliver, to_archive).

    - Already delivered or archived items are excluded from both lists.
    - Items older than ARCHIVE_AGE_DAYS are moved to to_archive, not to_deliver.
    - Remaining undelivered items are scored and the top MAX_DELIVER_PER_RUN are selected.
    """
    unhandled = [i for i in items if not is_delivered(i) and not is_archived(i)]

    to_archive = [i for i in unhandled if age_days(i, reference) > ARCHIVE_AGE_DAYS]
    candidates = [i for i in unhandled if age_days(i, reference) <= ARCHIVE_AGE_DAYS]

    scored = sorted(candidates, key=lambda i: priority_score(i, reference), reverse=True)
    to_deliver = scored[:MAX_DELIVER_PER_RUN]

    return to_deliver, to_archive


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------

def load_queue(path: Path) -> list[dict]:
    """Load and parse the queue JSON. Returns empty list if file is missing or malformed."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(path: Path, items: list[dict]) -> None:
    """Write the queue back to disk, pretty-printed."""
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mark_delivered(item: dict, timestamp: str) -> dict:
    """Return a new item dict with delivered=True and delivered_at set. Pure."""
    return {**item, "delivered": True, "delivered_at": timestamp}


def mark_archived(item: dict, timestamp: str) -> dict:
    """Return a new item dict with archived=True and archived_at set. Pure."""
    return {**item, "archived": True, "archived_at": timestamp, "archive_reason": "age_exceeded_14_days"}


def apply_updates(
    items: list[dict],
    delivered: list[dict],
    archived: list[dict],
    timestamp: str,
) -> list[dict]:
    """
    Return a new items list with delivered and archived items updated.

    Matching strategy:
    - Items with a source_id are matched by source_id value (fast, reliable).
    - Items without a source_id are matched by object identity (id()), using
      the same list references passed in from select_items. This handles
      queue entries written before source_id was a required field.

    Pure: does not mutate the input list.
    """
    delivered_ids = {i["source_id"] for i in delivered if i.get("source_id")}
    archived_ids = {i["source_id"] for i in archived if i.get("source_id")}
    # Object-identity fallback for items that lack source_id
    delivered_objs = {id(i) for i in delivered if not i.get("source_id")}
    archived_objs = {id(i) for i in archived if not i.get("source_id")}

    def update(item: dict) -> dict:
        sid = item.get("source_id")
        if sid and sid in delivered_ids:
            return mark_delivered(item, timestamp)
        if sid and sid in archived_ids:
            return mark_archived(item, timestamp)
        if id(item) in delivered_objs:
            return mark_delivered(item, timestamp)
        if id(item) in archived_objs:
            return mark_archived(item, timestamp)
        return item

    return [update(i) for i in items]


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_item(item: dict, index: int, total: int) -> str:
    """
    Format a single queue item as a Telegram message.

    Uses plain text (no markdown) for Telegram compatibility.
    """
    source_file = item.get("source_file", "unknown source")
    source_label = {
        "meta/premise-review.md": "Premise Review",
        "meta/hygiene-review.md": "Hygiene Review",
        "meta/oracle/learnings.md": "Oracle Learnings",
    }.get(source_file, source_file)

    queued = item.get("queued_at", "")
    if queued:
        try:
            dt = datetime.fromisoformat(queued.replace("Z", "+00:00"))
            queued_label = dt.strftime("%Y-%m-%d")
        except ValueError:
            queued_label = queued
    else:
        queued_label = "unknown date"

    observation = item.get("observation", "").strip()
    surface_reason = item.get("surface_reason", "").strip()

    lines = [
        f"Reflective surface item {index}/{total} — {source_label} ({queued_label})",
        "",
        observation,
    ]

    if surface_reason:
        lines += ["", f"Why surfaced: {surface_reason}"]

    return "\n".join(lines)


def format_summary(delivered_count: int, archived_count: int, remaining_count: int) -> str:
    """Format a brief job summary for task output."""
    parts = [f"Delivered: {delivered_count} item(s)."]
    if archived_count:
        parts.append(f"Archived (age > 14 days): {archived_count} item(s).")
    if remaining_count:
        parts.append(f"Remaining undelivered: {remaining_count} item(s).")
    else:
        parts.append("Queue is clear.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Inbox / task-output I/O helpers
# ---------------------------------------------------------------------------

def _inbox_dir() -> Path:
    """Return the inbox directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    inbox = Path(messages_base) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _task_outputs_dir() -> Path:
    """Return the task-outputs directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    task_outputs = Path(messages_base) / "task-outputs"
    task_outputs.mkdir(parents=True, exist_ok=True)
    return task_outputs


def write_inbox_message(chat_id: int, text: str, timestamp: str) -> None:
    """
    Write a single subagent_result message to the inbox.
    The dispatcher picks it up and routes it via send_reply.
    Pure side-effect boundary: all I/O is isolated here.
    """
    inbox = _inbox_dir()
    msg_id = f"surface_queue_delivery_{uuid.uuid4().hex}"
    msg = {
        "id": msg_id,
        "type": "subagent_result",
        "task_id": msg_id,
        "chat_id": chat_id,
        "source": "telegram",
        "text": text,
        "status": "success",
        "sent_reply_to_user": False,
        "timestamp": timestamp,
    }
    out_path = inbox / f"{msg_id}.json"
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


def write_task_output(output: str, status: str, timestamp: str) -> None:
    """
    Write a task output record directly to the task-outputs directory.
    Mirrors the format expected by check_task_outputs.
    """
    task_outputs = _task_outputs_dir()
    # Use a timestamp-based filename consistent with existing task output files
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-surface-queue-delivery.json"
    record = {
        "job_name": "surface-queue-delivery",
        "timestamp": timestamp,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


# ---------------------------------------------------------------------------
# Delivery via direct inbox writes
# ---------------------------------------------------------------------------

def deliver(messages: list[str], job_summary: str, delivered_count: int, archived_count: int) -> None:
    """
    Deliver messages to the Lobster inbox and write task output.
    Each message is written as a subagent_result inbox file; the dispatcher
    picks them up and routes them via send_reply. No Claude subprocess is spawned.
    Side effects are isolated to this function.
    """
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for msg_text in messages:
        write_inbox_message(chat_id, msg_text, timestamp)
    write_task_output(job_summary, "success", timestamp)


def deliver_no_items(reason: str) -> None:
    """Write a task output record when there is nothing to deliver."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_task_output(reason, "success", timestamp)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run() -> int:
    """
    Execute the surface queue delivery pipeline.

    Reads queue -> scores undelivered items -> selects top N -> formats messages
    -> writes inbox files for dispatcher delivery -> marks delivered -> saves queue.

    Returns exit code: 0 for success, 1 for failure.
    """
    reference = now_utc()
    timestamp_iso = reference.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[{timestamp_iso}] Starting surface-queue-delivery")
    print(f"  Queue path: {QUEUE_PATH}")

    items = load_queue(QUEUE_PATH)
    print(f"  Total items in queue: {len(items)}")

    to_deliver, to_archive = select_items(items, reference)
    print(f"  Items to deliver: {len(to_deliver)}")
    print(f"  Items to archive (age > {ARCHIVE_AGE_DAYS} days): {len(to_archive)}")

    if not to_deliver and not to_archive:
        print("  Nothing to do.")
        deliver_no_items("No undelivered items in the reflective surface queue.")
        return 0

    # Format messages
    messages = [
        format_item(item, i + 1, len(to_deliver))
        for i, item in enumerate(to_deliver)
    ]

    # Count remaining after this run
    all_undelivered = [i for i in items if not is_delivered(i) and not is_archived(i)]
    remaining_after = max(0, len(all_undelivered) - len(to_deliver) - len(to_archive))

    job_summary = format_summary(len(to_deliver), len(to_archive), remaining_after)
    print(f"  Summary: {job_summary}")

    # Deliver
    if messages:
        print("  Writing messages to inbox for dispatcher delivery...")
        deliver(messages, job_summary, len(to_deliver), len(to_archive))
    else:
        # Only archiving, no messages to send — still write task output
        deliver_no_items(job_summary)

    # Mark items in queue and save
    updated_items = apply_updates(items, to_deliver, to_archive, timestamp_iso)
    save_queue(QUEUE_PATH, updated_items)
    print(f"  Queue saved with {len(to_deliver)} delivered and {len(to_archive)} archived.")

    print(f"[{timestamp_iso}] surface-queue-delivery complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
