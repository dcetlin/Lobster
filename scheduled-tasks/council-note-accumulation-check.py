#!/usr/bin/env python3
"""
Council note-accumulation trigger.

Type B cron script — runs every 30 minutes. Checks whether enough new ergonomics
notes have accumulated since the last council deliberation. When the threshold is
reached, writes a council-sweep inbox message so the dispatcher can spawn a
council-deliberation subagent.

Does NOT use an LLM — pure counting and file I/O.

Job: council-note-check (Type B)
Schedule: */30 * * * *
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LOBSTER_HOME = Path(os.environ.get("LOBSTER_HOME", Path.home() / "lobster"))
sys.path.insert(0, str(LOBSTER_HOME))

from src.utils.jobs import is_job_enabled  # noqa: E402 — path insert must come first

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NOTES_THRESHOLD = 5  # How many new notes trigger a council run

NOTES_DIR = Path.home() / "lobster-workspace/workstreams/ergonomics-orient/notes"
COUNCIL_STATE = Path.home() / "lobster-workspace/workstreams/agent-council/council-state.json"
INBOX_DIR = Path.home() / "messages/inbox"

ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0"))


def _load_council_state() -> dict:
    if COUNCIL_STATE.exists():
        return json.loads(COUNCIL_STATE.read_text())
    return {
        "last_deliberation_at": None,
        "notes_since_last_run": 0,
        "notes_processed_count": 0,
        "entries_committed_total": 0,
        "pending_queue": [],
        "runs": [],
    }


def _count_new_notes(last_run_ts: str | None) -> list[str]:
    """Return filenames of note files newer than last_run_ts."""
    if not NOTES_DIR.exists():
        return []

    if last_run_ts is None:
        # Never run — all notes are new
        return [f.name for f in sorted(NOTES_DIR.glob("*.md"))]

    cutoff = datetime.fromisoformat(last_run_ts.replace("Z", "+00:00"))
    new_notes = []
    for note in sorted(NOTES_DIR.glob("*.md")):
        mtime = datetime.fromtimestamp(note.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            new_notes.append(note.name)
    return new_notes


def _write_inbox_trigger(new_note_count: int, note_files: list[str]) -> None:
    """Write a council-sweep trigger message to the Lobster inbox."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc)
    msg_id = f"{int(ts.timestamp() * 1000)}_council_trigger"
    msg = {
        "id": msg_id,
        "type": "scheduled_reminder",
        "reminder_type": "council-sweep-trigger",
        "job_name": "council-sweep-trigger",
        "chat_id": ADMIN_CHAT_ID,
        "source": "system",
        "timestamp": ts.isoformat(),
        "text": (
            f"Council note-accumulation threshold reached: {new_note_count} new notes "
            f"since last deliberation (threshold: {NOTES_THRESHOLD}). "
            f"Trigger a council sweep on accumulated ergonomics notes."
        ),
        "task_content": (
            Path.home() / "lobster-workspace/scheduled-jobs/tasks/council-sunday-sweep.md"
        ).read_text()
        if (Path.home() / "lobster-workspace/scheduled-jobs/tasks/council-sunday-sweep.md").exists()
        else (
            f"Run a council deliberation sweep on the accumulated ergonomics notes.\n"
            f"New notes: {', '.join(note_files)}\n"
            f"Domain context: ~/lobster-workspace/workstreams/ergonomics-orient/frontier.md\n"
            f"Canon path: ~/lobster-workspace/workstreams/agent-council/canon/\n"
            f"Council state: ~/lobster-workspace/workstreams/agent-council/council-state.json\n"
        ),
        "metadata": {
            "new_note_count": new_note_count,
            "note_files": note_files,
            "threshold": NOTES_THRESHOLD,
        },
    }
    msg_path = INBOX_DIR / f"{msg_id}.json"
    msg_path.write_text(json.dumps(msg, indent=2))
    print(f"[council-note-check] Wrote inbox trigger: {msg_path.name} ({new_note_count} notes)")


def main() -> None:
    if not is_job_enabled("council-note-check"):
        print("[council-note-check] Job disabled — skipping")
        return

    state = _load_council_state()
    last_run_ts = state.get("last_deliberation_at")
    new_notes = _count_new_notes(last_run_ts)
    new_count = len(new_notes)

    print(f"[council-note-check] New notes since last run: {new_count} (threshold: {NOTES_THRESHOLD})")

    if new_count >= NOTES_THRESHOLD:
        _write_inbox_trigger(new_count, new_notes)
        # Update state to record that we've fired (prevents duplicate triggers)
        state["notes_since_last_run"] = 0
        COUNCIL_STATE.parent.mkdir(parents=True, exist_ok=True)
        COUNCIL_STATE.write_text(json.dumps(state, indent=2))
    else:
        # Record current count in state for observability
        state["notes_since_last_run"] = new_count
        COUNCIL_STATE.parent.mkdir(parents=True, exist_ok=True)
        COUNCIL_STATE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
