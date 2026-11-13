"""
src/utils/activity_gate.py — Pre-LLM activity gate for scheduled synthesis jobs.

Scheduled synthesis jobs (like async-deep-work) should call ``is_active()`` as
their very first step, before any LLM inference or expensive processing.  If the
gate returns False, the job should short-circuit immediately with a "quiet night
— skipped" write_task_output and exit.

Usage in a task prompt::

    import sys, subprocess, json
    result = subprocess.run(
        ["uv", "run", "-c",
         "import sys; sys.path.insert(0, '/home/lobster/lobster'); "
         "from src.utils.activity_gate import check_activity; "
         "import json; print(json.dumps(check_activity()))"],
        capture_output=True, text=True, cwd="/home/lobster/lobster"
    )
    gate = json.loads(result.stdout)
    if not gate["active"]:
        # write_task_output + exit — no LLM synthesis
        ...

Design decisions:
- Scans ~/messages/processed/ JSON files for the activity window (default 24 h).
  This directory is the canonical record of all processed messages.
- "Meaningful activity" = genuine user-initiated messages from a non-system source
  (type="text" with source != "system", or voice/photo messages from real users).
  Excludes: scheduled_reminder, subagent_notification, subagent_result,
  session_reconnect, compact_reminder, and all source="system" messages.
- Threshold (default: 3 genuine user messages in the window).
  Set conservatively — a single message exchange does NOT clear the bar.
  Three messages implies real back-and-forth and genuine engagement.
- Returns a dict with keys: active, message_count, window_hours, threshold.
  Callers check ``gate["active"]`` and log gate["reason"] for observability.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW_HOURS: int = 24
_DEFAULT_MIN_MESSAGES: int = 3

# Message types that indicate genuine user activity (not system noise)
_USER_ACTIVITY_TYPES: frozenset[str] = frozenset({
    "text",
    "photo",
    "voice",
    "document",
    "audio",
    "video",
    "sticker",
})

# Sources that are always system/cron noise — excluded from activity count
_NOISE_SOURCES: frozenset[str] = frozenset({
    "system",
    "cron",
    "local-claude",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_activity(
    *,
    window_hours: int = _DEFAULT_WINDOW_HOURS,
    min_messages: int = _DEFAULT_MIN_MESSAGES,
    processed_dir: str | Path | None = None,
) -> dict:
    """
    Check whether meaningful user activity occurred in the past ``window_hours``.

    Args:
        window_hours:  How far back to look (default 24).
        min_messages:  Minimum genuine user messages to be considered "active"
                       (default 3).
        processed_dir: Override the processed-messages directory
                       (default ~/messages/processed/).

    Returns:
        dict with keys:
            active        (bool)  — True if activity threshold was met
            message_count (int)   — number of qualifying user messages found
            window_hours  (int)   — the window used
            threshold     (int)   — the min_messages threshold used
            reason        (str)   — human-readable explanation
    """
    base_dir = Path(processed_dir) if processed_dir else Path.home() / "messages" / "processed"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    count = _count_user_messages(base_dir, cutoff)

    active = count >= min_messages
    if active:
        reason = f"{count} genuine user message(s) in the past {window_hours}h (threshold: {min_messages})"
    else:
        reason = (
            f"only {count} genuine user message(s) in the past {window_hours}h "
            f"(threshold: {min_messages}) — quiet window, skipping synthesis"
        )

    return {
        "active": active,
        "message_count": count,
        "window_hours": window_hours,
        "threshold": min_messages,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_user_messages(processed_dir: Path, cutoff: datetime) -> int:
    """
    Scan processed-message JSON files and return the count of genuine user
    messages at or after ``cutoff``.

    Files are named with a timestamp prefix (e.g. 1794584715876_13382.json);
    files whose mtime predates the cutoff by more than 1 hour are skipped for
    efficiency (mtime is used only as a cheap pre-filter; the JSON timestamp
    is the authoritative check).
    """
    if not processed_dir.exists():
        return 0

    count = 0
    cutoff_mtime = cutoff.timestamp() - 3600  # 1h grace for clock drift

    for entry in processed_dir.iterdir():
        if not entry.name.endswith(".json"):
            continue
        # Cheap mtime pre-filter — skip obviously old files
        try:
            if entry.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue

        try:
            with entry.open() as fh:
                msg = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        if _is_user_activity(msg, cutoff):
            count += 1

    return count


def _is_user_activity(msg: dict, cutoff: datetime) -> bool:
    """
    Return True if ``msg`` represents genuine user-initiated activity after
    ``cutoff``.
    """
    # Must be a user-activity type (not a system/cron message type)
    msg_type = (msg.get("type") or "").lower()
    if msg_type not in _USER_ACTIVITY_TYPES:
        return False

    # Exclude known noise sources
    source = (msg.get("source") or "").lower()
    if source in _NOISE_SOURCES:
        return False

    # Check timestamp — try "timestamp" field first, then "created_at"
    ts_raw = msg.get("timestamp") or msg.get("created_at") or ""
    if not ts_raw:
        return False

    try:
        ts = _parse_timestamp(str(ts_raw))
    except (ValueError, TypeError):
        return False

    return ts >= cutoff


def _parse_timestamp(ts: str) -> datetime:
    """
    Parse an ISO 8601 timestamp string into an aware datetime.

    Handles trailing Z, +00:00, and naive datetimes (assumed UTC).
    """
    # Normalise Z suffix
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        # Fallback: strip fractional seconds and retry
        base = ts.split(".")[0]
        dt = datetime.fromisoformat(base)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt
