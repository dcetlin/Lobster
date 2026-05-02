"""
Circadian-aware message delivery for Lobster.

Non-urgent scheduled-job outputs are held in a local queue and batched for
delivery during Dan's morning window (06:00–10:00 America/Los_Angeles).
Urgent messages (user replies, incident alerts) always deliver immediately.

Public API
----------
is_non_urgent(message: dict) -> bool
is_morning_window() -> bool
queue_message(chat_id, text, source, source_type) -> None
flush_morning_queue(send_fn) -> int
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Keywords that signal an urgent job output (health failures, system alerts).
# Checked case-insensitively against message text.
_URGENT_KEYWORDS: frozenset[str] = frozenset([
    "health check failure",
    "health check failed",
    "starvation detected",
    "heartbeat stale",
    "error:",
    "alert:",
    "incident",
    "critical failure",
    "system error",
])


def _queue_path() -> Path:
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "data" / "pending-deliveries.jsonl"


def is_morning_window() -> bool:
    """True if current Pacific time is between 06:00 and 10:00 (inclusive of 06, exclusive of 10)."""
    now_pt = datetime.now(_PACIFIC)
    return 6 <= now_pt.hour < 10


def is_non_urgent(message: dict) -> bool:
    """
    True if the message is safe to defer to the morning delivery window.

    Non-urgent when ALL conditions hold:
    - type is "subagent_result" (scheduled job output, not a direct user reply)
    - no reply_to_message_id (not threaded to a user message)
    - text does not contain urgent-signal keywords (health failures, incidents)
    """
    if message.get("type") != "subagent_result":
        return False
    if message.get("reply_to_message_id"):
        return False
    text_lower = message.get("text", "").lower()
    if any(kw in text_lower for kw in _URGENT_KEYWORDS):
        return False
    return True


def queue_message(
    chat_id: int,
    text: str,
    source: str = "",
    source_type: str = "scheduled_job",
) -> None:
    """Append a pending-delivery entry to the JSONL queue."""
    entry = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "text": text,
        "source": source,
        "source_type": source_type,
        "delivered": False,
    }
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def flush_morning_queue(send_fn) -> int:
    """
    Deliver all pending queue entries by calling send_fn(chat_id, text).

    Rewrites the JSONL file atomically with delivered=true for each sent entry.
    Returns the count of messages delivered. Entries that fail to send are left
    as undelivered and retried on the next flush.
    """
    path = _queue_path()
    if not path.exists():
        return 0

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    delivered_count = 0
    updated: list[dict] = []
    for entry in entries:
        if entry.get("delivered", False):
            updated.append(entry)
            continue
        try:
            send_fn(entry["chat_id"], entry["text"])
            updated.append({**entry, "delivered": True})
            delivered_count += 1
        except Exception:
            updated.append(entry)

    tmp_path = Path(str(path) + ".tmp")
    content = "\n".join(json.dumps(e, ensure_ascii=False) for e in updated)
    if content:
        content += "\n"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)

    return delivered_count
