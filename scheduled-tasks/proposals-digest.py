#!/usr/bin/env python3
"""
Proposals Digest — Lobster Scheduled Job
=========================================

Reads meta/proposals.md, finds entries that have not yet been delivered,
and surfaces them to the user via the Lobster inbox. Marks delivered entries
so they are not re-sent on subsequent runs.

A "delivered" entry is tracked by appending a `<!-- delivered: YYYY-MM-DD -->`
HTML comment on the line immediately after its heading.

Delivery state is purely file-based — no external database required. The file
is the source of truth.

Run standalone:
    uv run ~/lobster/scheduled-tasks/proposals-digest.py
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROPOSALS_PATH = Path.home() / "lobster-workspace" / "meta" / "proposals.md"
JOB_NAME = "proposals-digest"

# Maximum number of entries to deliver per run. Keeps Telegram from flooding.
MAX_DELIVER_PER_RUN = 2

# Heading pattern that opens a meta-run entry: ### [YYYY-MM-DD] ...
ENTRY_HEADING_RE = re.compile(r"^### \[(\d{4}-\d{2}-\d{2})\](.+)$", re.MULTILINE)

# Marker we write into the file to record delivery.
DELIVERED_MARKER_PREFIX = "<!-- proposals-digest-delivered:"
DELIVERED_MARKER_RE = re.compile(
    r"<!-- proposals-digest-delivered: (\d{4}-\d{2}-\d{2}) -->"
)


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Proposals file parsing
# ---------------------------------------------------------------------------

def parse_entries(content: str) -> list[dict]:
    """
    Parse proposals.md into a list of entry dicts.

    Each entry has:
      - date: str  (YYYY-MM-DD from heading)
      - title: str (rest of heading after date)
      - heading_pos: int (character offset of the heading start)
      - body_start: int (character offset of the line after heading)
      - body_end: int (character offset where next entry begins, or end of file)
      - delivered: bool (True if a delivered marker is present in the body)
      - delivered_date: str | None
    """
    entries = []
    matches = list(ENTRY_HEADING_RE.finditer(content))

    for idx, m in enumerate(matches):
        date_str = m.group(1)
        title = m.group(2).strip()
        heading_pos = m.start()

        # Body starts at the character after the heading line's newline
        body_start = m.end() + 1  # +1 to skip the \n after the heading

        # Body ends at the start of the next entry heading, or end of file
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)

        body = content[body_start:body_end]

        delivered_match = DELIVERED_MARKER_RE.search(body)
        delivered = delivered_match is not None
        delivered_date = delivered_match.group(1) if delivered_match else None

        entries.append({
            "date": date_str,
            "title": title,
            "heading_pos": heading_pos,
            "body_start": body_start,
            "body_end": body_end,
            "body": body,
            "delivered": delivered,
            "delivered_date": delivered_date,
        })

    return entries


def undelivered_entries(entries: list[dict]) -> list[dict]:
    """Return entries that have not yet been delivered, oldest first."""
    return [e for e in entries if not e["delivered"]]


def select_to_deliver(entries: list[dict]) -> list[dict]:
    """Select up to MAX_DELIVER_PER_RUN oldest undelivered entries."""
    # entries are already in file order (chronological, oldest first in typical append style)
    return entries[:MAX_DELIVER_PER_RUN]


# ---------------------------------------------------------------------------
# File mutation
# ---------------------------------------------------------------------------

def insert_delivered_marker(content: str, entry: dict, date_str: str) -> str:
    """
    Return a new content string with a delivered marker inserted at the start
    of the entry's body. Pure: does not mutate content.
    """
    marker = f"{DELIVERED_MARKER_PREFIX} {date_str} -->\n"
    insert_pos = entry["body_start"]
    return content[:insert_pos] + marker + content[insert_pos:]


def mark_all_delivered(content: str, entries: list[dict], date_str: str) -> str:
    """
    Insert delivered markers for all given entries.

    Processes entries in reverse body_start order so that earlier insertions
    do not shift the character offsets of later ones.
    Pure: returns a new string.
    """
    result = content
    offset = 0  # cumulative character offset from prior insertions

    # Sort by body_start ascending so we process in document order,
    # then apply with a running offset to keep positions accurate.
    sorted_entries = sorted(entries, key=lambda e: e["body_start"])
    marker_len = len(f"{DELIVERED_MARKER_PREFIX} {date_str} -->\n")

    for entry in sorted_entries:
        adjusted_entry = {**entry, "body_start": entry["body_start"] + offset}
        result = insert_delivered_marker(result, adjusted_entry, date_str)
        offset += marker_len

    return result


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_entry(entry: dict, index: int, total: int) -> str:
    """
    Format a single proposals entry as a Telegram message.

    Strips the delivered marker (if somehow present) from the body before
    formatting. Uses plain text for Telegram compatibility.
    """
    body = entry["body"].strip()
    # Remove any existing delivered markers (defensive)
    body = DELIVERED_MARKER_RE.sub("", body).strip()

    date_str = entry["date"]
    title = entry["title"]

    header = f"Proposals digest {index}/{total} — {date_str}: {title}"

    return "\n".join([header, "", body])


def format_task_summary(delivered: int, remaining: int) -> str:
    parts = [f"Delivered: {delivered} proposal entry(ies)."]
    if remaining:
        parts.append(f"Remaining undelivered: {remaining}.")
    else:
        parts.append("All entries delivered.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Inbox / task-output I/O helpers
# ---------------------------------------------------------------------------

def _inbox_dir() -> Path:
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    inbox = Path(messages_base) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _task_outputs_dir() -> Path:
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    task_outputs = Path(messages_base) / "task-outputs"
    task_outputs.mkdir(parents=True, exist_ok=True)
    return task_outputs


def write_inbox_message(chat_id: int, text: str, ts: str) -> None:
    """
    Write a subagent_result message to the Lobster inbox.
    The dispatcher picks it up and routes it via send_reply.
    All I/O is isolated to this function.
    """
    inbox = _inbox_dir()
    msg_id = f"proposals_digest_{uuid.uuid4().hex}"
    msg = {
        "id": msg_id,
        "type": "subagent_result",
        "task_id": msg_id,
        "chat_id": chat_id,
        "source": "telegram",
        "text": text,
        "status": "success",
        "sent_reply_to_user": False,
        "timestamp": ts,
    }
    out_path = inbox / f"{msg_id}.json"
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


def write_task_output(output: str, status: str, ts: str) -> None:
    task_outputs = _task_outputs_dir()
    date_prefix = ts[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-proposals-digest.json"
    record = {
        "job_name": JOB_NAME,
        "timestamp": ts,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run() -> int:
    """
    Execute the proposals digest pipeline.

    Reads proposals.md → finds undelivered entries → selects up to
    MAX_DELIVER_PER_RUN → formats messages → writes inbox files for dispatcher
    delivery → inserts delivered markers → saves proposals.md.

    Returns exit code: 0 for success, 1 for failure.
    """
    now = now_utc()
    ts = timestamp_iso(now)
    date_today = today_str(now)

    print(f"[{ts}] Starting proposals-digest")
    print(f"  Proposals path: {PROPOSALS_PATH}")

    if not PROPOSALS_PATH.exists():
        msg = "proposals.md not found — nothing to deliver."
        print(f"  {msg}")
        write_task_output(msg, "success", ts)
        return 0

    content = PROPOSALS_PATH.read_text(encoding="utf-8")
    entries = parse_entries(content)
    print(f"  Total entries parsed: {len(entries)}")

    pending = undelivered_entries(entries)
    print(f"  Undelivered entries: {len(pending)}")

    if not pending:
        msg = "No undelivered proposals entries. Queue is clear."
        print(f"  {msg}")
        write_task_output(msg, "success", ts)
        return 0

    to_deliver = select_to_deliver(pending)
    remaining_after = len(pending) - len(to_deliver)

    print(f"  Delivering {len(to_deliver)} entry(ies)...")

    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

    messages = [
        format_entry(entry, i + 1, len(to_deliver))
        for i, entry in enumerate(to_deliver)
    ]

    for msg_text in messages:
        write_inbox_message(chat_id, msg_text, ts)

    # Mark delivered entries in the file
    updated_content = mark_all_delivered(content, to_deliver, date_today)
    PROPOSALS_PATH.write_text(updated_content, encoding="utf-8")
    print(f"  Marked {len(to_deliver)} entry(ies) as delivered in proposals.md")

    summary = format_task_summary(len(to_deliver), remaining_after)
    print(f"  {summary}")
    write_task_output(summary, "success", ts)

    print(f"[{ts}] proposals-digest complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
