"""
src/classifiers/checkpoint.py — Checkpoint-based event-driven triggering for classifiers.

Purpose
-------
Eliminates no-op classifier cycles by persisting a "last seen max event id" after each
run. On the next invocation, the classifier checks whether any new events have been
written to memory.db since that checkpoint. If not, it exits immediately — no DB
classification work, no LLM inference, no log noise.

Design
------
All functions are pure or have clearly isolated side effects:

- `load_checkpoint(path)` — pure read; returns a dict with `max_event_id` (int) and
  `checked_at` (ISO string). Returns a sentinel dict with max_event_id=-1 if the file
  does not exist (meaning: run unconditionally on first invocation).

- `save_checkpoint(path, max_event_id)` — isolated write; atomically replaces the
  checkpoint file via a tmp-file rename so partial writes never corrupt state.

- `count_new_events(conn, since_id)` — DB read; returns the count of events with
  id > since_id. Pure relative to the connection — no writes.

- `should_run(checkpoint_path, db_path)` — composes the above; returns True if there
  are new events to classify. Handles missing DB, missing checkpoint, and DB errors
  gracefully (defaults to True on any uncertainty, so the classifier runs rather than
  silently skipping).

Usage
-----
Each classifier calls `should_run(checkpoint_path, db_path)` at entry. If it returns
False, the script logs a one-liner and exits with code 0. After a successful pass, the
classifier calls `save_checkpoint(checkpoint_path, max_id_seen)` to advance the cursor.

The checkpoint file is a small JSON file stored at:
  $LOBSTER_WORKSPACE/data/<classifier-name>.checkpoint.json

It is not committed to git (runtime data), and is safe to delete — deletion causes the
next invocation to run unconditionally, which is correct.

See Also
--------
- src/classifiers/quick_classifier.py (Layer 3 — uses this module)
- src/classifiers/slow_reclassifier.py (Layer 4 — uses this module)
- GitHub: dcetlin/Lobster#74
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Sentinel: checkpoint file not found / first run — always run.
_NO_CHECKPOINT_ID = -1


def load_checkpoint(path: Path) -> dict:
    """
    Read checkpoint state from `path`.

    Returns a dict with keys:
        max_event_id (int): highest event id seen in the last pass.
                            -1 if no checkpoint exists (first run).
        checked_at (str):   ISO timestamp of when the checkpoint was written.
                            Empty string if no checkpoint exists.

    Never raises — returns the sentinel dict on any read/parse error.
    """
    if not path.exists():
        return {"max_event_id": _NO_CHECKPOINT_ID, "checked_at": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "max_event_id": int(data.get("max_event_id", _NO_CHECKPOINT_ID)),
            "checked_at": str(data.get("checked_at", "")),
        }
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Could not read checkpoint %s (%s) — running unconditionally.", path, exc)
        return {"max_event_id": _NO_CHECKPOINT_ID, "checked_at": ""}


def save_checkpoint(path: Path, max_event_id: int) -> None:
    """
    Atomically write the checkpoint file at `path`.

    Uses a temp-file rename so a crash mid-write never leaves a corrupt file.
    Creates parent directories if they do not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "max_event_id": max_event_id,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    )
    # Write to a sibling tmp file then rename — atomic on POSIX.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    log.debug("Checkpoint saved: max_event_id=%d at %s", max_event_id, path)


def count_new_events(conn: sqlite3.Connection, since_id: int) -> int:
    """
    Return the count of events in memory.db with id > `since_id`.

    `since_id` of -1 means "no prior checkpoint" — returns total event count,
    so the classifier always runs on first invocation.

    Raises sqlite3.OperationalError if the events table is unreadable (e.g. on a
    fresh install). The caller (should_run) catches this and fails open, returning
    True so the classifier runs rather than silently skipping a pass.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE id > ?", (since_id,)
    ).fetchone()
    return row[0] if row else 0


def max_event_id(conn: sqlite3.Connection) -> int:
    """
    Return the current maximum event id in memory.db.

    Returns -1 if the events table is empty or does not exist. This value is
    stored as the checkpoint after a successful pass so the next invocation
    knows where to pick up.
    """
    try:
        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return _NO_CHECKPOINT_ID
    except sqlite3.OperationalError as exc:
        log.debug("max_event_id: could not read events table (%s)", exc)
        return _NO_CHECKPOINT_ID


def should_run(checkpoint_path: Path, db_path: Path) -> bool:
    """
    Return True if there are new events to classify since the last checkpoint.

    Decision table:
    ┌─────────────────────────────┬────────────┬──────────────────────────────┐
    │ Condition                   │ Returns    │ Reason                       │
    ├─────────────────────────────┼────────────┼──────────────────────────────┤
    │ DB does not exist           │ False      │ Nothing to classify yet      │
    │ No checkpoint (first run)   │ True       │ Always run on first pass     │
    │ new events > 0              │ True       │ Work to do                   │
    │ new events == 0             │ False      │ No-op cycle — skip           │
    │ DB read error               │ True       │ Fail open — don't skip runs  │
    └─────────────────────────────┴────────────┴──────────────────────────────┘

    Fails open: on any unexpected error, returns True so the classifier runs
    rather than silently skipping a potentially important pass.
    """
    if not db_path.exists():
        log.info("memory.db not found at %s — nothing to classify.", db_path)
        return False

    checkpoint = load_checkpoint(checkpoint_path)
    since_id = checkpoint["max_event_id"]

    # First run: no prior checkpoint — always run.
    if since_id == _NO_CHECKPOINT_ID:
        log.debug("No checkpoint found — running unconditionally (first pass).")
        return True

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            new_count = count_new_events(conn, since_id)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("should_run: DB error (%s) — failing open, will run.", exc)
        return True

    if new_count == 0:
        log.info(
            "No new events since checkpoint (max_event_id=%d, checked_at=%s) — skipping.",
            since_id,
            checkpoint["checked_at"],
        )
        return False

    log.debug(
        "%d new event(s) since checkpoint (max_event_id=%d) — will run.",
        new_count,
        since_id,
    )
    return True
