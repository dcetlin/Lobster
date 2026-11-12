#!/usr/bin/env python3
"""check-inflight-reminders.py — Detect stale in-flight subagent work.

Runs every 3 minutes via cron. Reads ~/lobster-workspace/data/inflight-work.jsonl,
identifies entries that have been running longer than 2x their expected duration,
and drops a reminder message into ~/messages/inbox/ for the dispatcher to act on.

Staleness condition: (now - started_at) > expected_done_in_minutes * STALENESS_MULTIPLIER

Entries with completed_at or reminded_at are skipped to prevent duplicate pings.
The jsonl file is rewritten atomically with reminded_at timestamps on triggered entries.
"""

from __future__ import annotations

import sys
import os
_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Named constants derived from spec (issue #1686)
# ---------------------------------------------------------------------------

DEFAULT_EXPECTED_DONE_MINUTES: float = 30.0
"""Default staleness budget when expected_done_in_minutes is not present in the entry."""

STALENESS_MULTIPLIER: float = 2.0
"""Work is considered stale when elapsed time exceeds this multiple of the expected duration."""

REMINDER_TYPE: str = "inflight_stale"

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def is_stale(entry: dict[str, Any], now: datetime) -> bool:
    """Return True if the entry has been running longer than its staleness threshold.

    Pure function — no side effects, no I/O.
    """
    started_at_str = entry.get("started_at")
    if not started_at_str:
        return False

    try:
        started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False

    elapsed_minutes = (now - started_at).total_seconds() / 60.0
    expected = float(entry.get("expected_done_in_minutes") or DEFAULT_EXPECTED_DONE_MINUTES)
    threshold = expected * STALENESS_MULTIPLIER
    return elapsed_minutes > threshold


def should_remind(entry: dict[str, Any], now: datetime) -> bool:
    """Return True if this entry should generate a reminder.

    Conditions:
    - No completed_at (not done)
    - No reminded_at (not already reminded)
    - is_stale() is True

    Pure function — no side effects, no I/O.
    """
    if "completed_at" in entry:
        return False
    if entry.get("status") == "done":
        return False
    if "reminded_at" in entry:
        return False
    return is_stale(entry, now)


def mark_reminded(entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return a new entry dict with reminded_at set. Does not mutate the input.

    Pure function — returns a new dict.
    """
    reminded_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**entry, "reminded_at": reminded_at}


def build_reminder_message(entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Build the inbox message dict for a stale entry.

    The message is always routed to chat_id=0 (dispatcher), regardless of the
    original entry's chat_id. The dispatcher decides what (if anything) to
    surface to the user.

    Pure function — no side effects, no I/O.
    """
    task_id = entry.get("task_id", "unknown")
    description = entry.get("description", "(no description)")

    # Compute elapsed minutes for human-readable message
    started_at_str = entry.get("started_at", "")
    elapsed_str = "unknown"
    try:
        started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        elapsed_minutes = int((now - started_at).total_seconds() / 60)
        elapsed_str = f"{elapsed_minutes} min"
    except (ValueError, AttributeError):
        pass

    expected = entry.get("expected_done_in_minutes", DEFAULT_EXPECTED_DONE_MINUTES)

    text = (
        f"[Stale subagent] task_id={task_id} has been running for {elapsed_str} "
        f"(expected ~{expected} min). Description: {description}. "
        "Check whether the subagent completed without calling write_result."
    )

    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    epoch_ms = int(now.timestamp() * 1000)
    msg_id = f"{epoch_ms}_reminder_{REMINDER_TYPE}_{task_id}"

    return {
        "id": msg_id,
        "type": "scheduled_reminder",
        "reminder_type": REMINDER_TYPE,
        "source": "system",
        "chat_id": 0,
        "task_id": task_id,
        "text": text,
        "timestamp": timestamp,
    }


def resolve_final_states(
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve the final state of each task_id from an append-only entry list.

    The inflight-work.jsonl file is append-only: a task_id may appear multiple
    times (first as "running", then as "done").  This function determines the
    final state per task_id using last-entry-wins semantics (matching
    dispatcher-state-stop.py).

    Returns a dict mapping task_id -> last entry for that task_id.
    Entries without a task_id are not included (they cannot be grouped).

    Pure function — no side effects.
    """
    final_by_tid: dict[str, dict[str, Any]] = {}

    for entry in entries:
        tid = entry.get("task_id")
        if tid:
            final_by_tid[tid] = entry

    return final_by_tid


def is_task_done(final_entry: dict[str, Any]) -> bool:
    """Return True if the final entry for a task_id indicates completion.

    Uses last-entry-wins: only the final entry's status matters, matching
    dispatcher-state-stop.py semantics.

    Pure function — no side effects.
    """
    return final_entry.get("status") == "done" or "completed_at" in final_entry


def process_entries(
    entries: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Process all entries, returning (reminder_messages, updated_entries).

    Resolves the final state per task_id first (so a "done" entry suppresses
    reminders for earlier "running" entries of the same task), but preserves
    ALL original entries in updated_entries to avoid truncating the append-only
    JSONL file.

    Only the entry selected for reminder gets reminded_at added. Entries whose
    task_id has a final "done" state are skipped entirely.

    Pure function — no I/O.
    """
    # Resolve final states to know which task_ids are done
    final_by_tid = resolve_final_states(entries)

    # Build set of task_ids that are done (last entry is done)
    done_task_ids: set[str] = {
        tid for tid, final in final_by_tid.items() if is_task_done(final)
    }

    messages: list[dict[str, Any]] = []
    updated_entries: list[dict[str, Any]] = []
    # Track which task_ids already got a reminder (only remind once per task_id)
    reminded_task_ids: set[str] = set()

    for entry in entries:
        tid = entry.get("task_id")

        # Skip staleness check if this task_id's final state is "done"
        if tid and tid in done_task_ids:
            updated_entries.append(entry)
            continue

        # For entries with a task_id, only check the final entry for staleness
        if tid and final_by_tid.get(tid) is not entry:
            updated_entries.append(entry)
            continue

        # Only remind once per task_id
        if tid and tid in reminded_task_ids:
            updated_entries.append(entry)
            continue

        if should_remind(entry, now):
            messages.append(build_reminder_message(entry, now))
            updated_entries.append(mark_reminded(entry, now))
            if tid:
                reminded_task_ids.add(tid)
        else:
            updated_entries.append(entry)

    return messages, updated_entries


# ---------------------------------------------------------------------------
# I/O functions (side effects isolated here)
# ---------------------------------------------------------------------------


def parse_entries(jsonl_path: str) -> list[dict[str, Any]]:
    """Read all entries from a JSONL file. Returns [] if file does not exist.

    Skips malformed lines rather than failing.
    """
    path = Path(jsonl_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[check-inflight-reminders] Skipping malformed line: {line[:80]}", file=sys.stderr)
    return entries


def write_entries(jsonl_path: str, entries: list[dict[str, Any]]) -> None:
    """Atomically rewrite a JSONL file with the given entries.

    Uses a temp file + rename to avoid partial writes.
    """
    path = Path(jsonl_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    if content:
        content += "\n"

    # Atomic write: write to temp file in same directory, then rename
    dir_ = str(path.parent)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".inflight-tmp-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def drop_inbox_message(
    msg: dict[str, Any],
    *,
    inbox_dir: str | None = None,
) -> None:
    """Write a reminder message JSON file to the inbox directory.

    inbox_dir defaults to ~/messages/inbox/ (production path).
    Accepts inbox_dir as a keyword argument for testability.
    """
    if inbox_dir is None:
        inbox_dir = os.path.expanduser("~/messages/inbox")

    inbox_path = Path(inbox_dir)
    inbox_path.mkdir(parents=True, exist_ok=True)

    msg_id = msg["id"]
    filename = inbox_path / f"{msg_id}.json"
    filename.write_text(json.dumps(msg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    now = datetime.now(tz=timezone.utc)
    from src.utils.jobs import is_job_enabled
    if not is_job_enabled("check-inflight-reminders"):
        return

    data_dir = os.environ.get("LOBSTER_DATA_DIR", os.path.expanduser("~/lobster-workspace/data"))
    jsonl_path = os.path.join(data_dir, "inflight-work.jsonl")

    entries = parse_entries(jsonl_path)
    if not entries:
        return

    messages, updated_entries = process_entries(entries, now)

    if not messages:
        return

    print(
        f"[check-inflight-reminders] {len(messages)} stale entr{'y' if len(messages) == 1 else 'ies'} found",
        file=sys.stderr,
    )

    # Write reminders to inbox
    for msg in messages:
        drop_inbox_message(msg)
        print(f"[check-inflight-reminders] Reminder written for task_id={msg['task_id']}", file=sys.stderr)

    # Persist reminded_at timestamps back to the file
    write_entries(jsonl_path, updated_entries)


if __name__ == "__main__":
    main()
