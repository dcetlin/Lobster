#!/usr/bin/env python3
"""
Obsidian <-> DB Bidirectional Sync

Reads ACTIVE TODOS.md from the obsidian vault and syncs human edits back
into the canonical self_action_items.db, then regenerates the file from DB
and commits it.

SOLE WRITER OF ACTIVE TODOS.md
-------------------------------
This script is the exclusive writer of ACTIVE TODOS.md in the Obsidian vault.
No other script or job (including any future LOS nightly sweep) should call
render_active_todos() or write to this file directly.

Rationale: a race condition arises if any other job regenerates ACTIVE TODOS.md
independently. The sequence that causes data loss:
  1. Dan checks off items in Obsidian on his Mac.
  2. Obsidian-git pushes the vault to the VPS.
  3. A second writer (e.g. LOS nightly at 3am) reads the DB and regenerates
     ACTIVE TODOS.md before this sync job runs — overwriting Dan's checkmarks.
  4. This sync job runs, finds no checkmarks in the file, and never marks those
     items done in the DB.

Keeping this script as the sole writer removes the race: checkmarks are always
read before the file is overwritten, in a single atomic pass.

# ACTIVE TODOS.md is written exclusively by todo_obsidian_sync.py to prevent
# the race condition where a second writer overwrites unsynced Obsidian checkmarks
# before this script can persist them to the DB.

Flow:
  1. git pull the obsidian vault (pull latest edits from Mac)
  2. Parse ACTIVE TODOS.md:
       - [x] items  → mark as done in DB (if currently open)
       - [ ] items not in DB → insert (source = 'obsidian:ACTIVE TODOS.md')
       - [ ] items in DB with mismatched priority band → update priority
  3. Regenerate ACTIVE TODOS.md from DB
  4. git commit + push the vault

All DB writes are idempotent — re-running produces the same state.

Type B (cron-direct) job. See jobs.json entry below.

jobs.json entry:
    {
        "name": "todo-obsidian-sync",
        "type": "B",
        "dispatch": "cron-direct",
        "schedule": "*/30 * * * *",
        "task_file": null,
        "enabled": true
    }

Cron entry:
    */30 * * * * cd ~/lobster && uv run scheduled-tasks/todo_obsidian_sync.py >> ~/lobster-workspace/scheduled-jobs/logs/todo-obsidian-sync.log 2>&1 # LOBSTER-TODO-OBSIDIAN-SYNC

Run standalone (for testing):
    uv run ~/lobster/scheduled-tasks/todo_obsidian_sync.py [--dry-run] [--vault PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import (
    ActionItemStatus,
    compute_dedup_key,
    connect,
    find_duplicate,
    get_item_by_id,
    get_subtasks,
    insert_action_item,
    mark_done,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("todo-obsidian-sync")

# ---------------------------------------------------------------------------
# Constants (named after spec requirements — never use magic literals)
# ---------------------------------------------------------------------------

JOB_NAME = "todo-obsidian-sync"

ACTIVE_TODOS_FILENAME = "✅ ACTIVE TODOS.md"
OBSIDIAN_SOURCE = "obsidian:ACTIVE TODOS.md"

# Priority band boundaries as defined in the design doc (Section 2 & 6)
PRIORITY_URGENT_MAX = 3    # P1–P3: Urgent / This Week
PRIORITY_ACTIVE_MAX = 6    # P4–P6: Active
# P7–P9: Someday / Aspirational (anything > PRIORITY_ACTIVE_MAX)

# Representative midpoints used when inserting new items (keeps them sortable
# within their band without colliding with existing DB priorities)
PRIORITY_URGENT_DEFAULT = 3
PRIORITY_ACTIVE_DEFAULT = 5
PRIORITY_SOMEDAY_DEFAULT = 8

_VAULT_DEFAULT = Path.home() / "lobster-workspace" / "obsidian-vault"
_DB_DEFAULT = Path.home() / "lobster-user-config" / "data" / "self_action_items.db"


def _get_workspace() -> Path:
    """Return the workspace path, reading LOBSTER_WORKSPACE at call time (not import time).

    Deferred to function call so tests can override via monkeypatch.setenv.
    """
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

# ---------------------------------------------------------------------------
# Data structures (immutable / pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedItem:
    """A single TODO item parsed from ACTIVE TODOS.md."""
    text: str
    dedup_key: str
    priority: int                    # representative priority for this item's band
    workstream: Optional[str]
    item_id: Optional[int] = None    # extracted from <!-- id:N --> comment, if present
    parent_id: Optional[int] = None  # extracted from <!-- parent:N --> comment, if present


@dataclass
class ParsedTodos:
    """Output of parse_active_todos — split into open and done lists."""
    open: list[ParsedItem] = field(default_factory=list)
    done: list[ParsedItem] = field(default_factory=list)


@dataclass
class SyncResult:
    """Summary of what sync_obsidian_to_db did during a run."""
    done_count: int = 0
    inserted_count: int = 0
    priority_changed_count: int = 0
    skipped_already_done: int = 0

    def __str__(self) -> str:
        return (
            f"done={self.done_count} inserted={self.inserted_count} "
            f"priority_changed={self.priority_changed_count} "
            f"skipped_already_done={self.skipped_already_done}"
        )


# ---------------------------------------------------------------------------
# Parsing (pure functions — no I/O or DB access)
# ---------------------------------------------------------------------------

# Section header patterns used to assign priority bands
_URGENT_HEADER_RE = re.compile(r"##\s+Urgent\s*/\s*This Week", re.IGNORECASE)
_ACTIVE_HEADER_RE = re.compile(r"##\s+Active\s*\(P4", re.IGNORECASE)
_SOMEDAY_HEADER_RE = re.compile(r"##\s+Someday\s*/\s*Aspirational", re.IGNORECASE)

# Workstream subsection: ### <name>
_WORKSTREAM_SECTION_RE = re.compile(r"^###\s+(\S+)", re.MULTILINE)

# Top-level checkbox line: "- [ ] text" or "- [x] text" (no leading spaces)
_CHECKBOX_RE = re.compile(r"^- \[(?P<checked>[ xX])\]\s+(?P<text>.+)$")

# Subtask checkbox line: "  - [ ] text" or "  - [x] text" (exactly 2-space indent)
_SUBTASK_CHECKBOX_RE = re.compile(r"^  - \[(?P<checked>[ xX])\]\s+(?P<text>.+)$")

# Trailing workstream annotation: "  *(workstream)*" at end of line
_ANNOTATION_RE = re.compile(r"\s+\*\([^)]+\)\*\s*$")

# HTML comment: <!-- id:N --> or <!-- id:N parent:P -->
_ID_COMMENT_RE = re.compile(r"<!--\s*id:(?P<id>\d+)(?:\s+parent:(?P<parent>\d+))?\s*-->")

# Strip HTML comment from text
_HTML_COMMENT_RE = re.compile(r"\s*<!--[^>]*-->")


def _priority_for_section(section: str) -> int:
    """Map a section name to the representative priority for that band."""
    mapping = {
        "urgent": PRIORITY_URGENT_DEFAULT,
        "active": PRIORITY_ACTIVE_DEFAULT,
        "someday": PRIORITY_SOMEDAY_DEFAULT,
    }
    return mapping.get(section, PRIORITY_ACTIVE_DEFAULT)


def _is_in_same_priority_band(db_priority: int, file_priority: int) -> bool:
    """Return True if db_priority and file_priority fall in the same band."""
    def _band(p: int) -> str:
        if p <= PRIORITY_URGENT_MAX:
            return "urgent"
        elif p <= PRIORITY_ACTIVE_MAX:
            return "active"
        else:
            return "someday"
    return _band(db_priority) == _band(file_priority)


def _parse_item_text(raw_text: str) -> tuple[str, Optional[int], Optional[int]]:
    """Extract clean text, item_id, and parent_id from a raw checkbox text.

    Returns (text, item_id, parent_id).
    HTML comments (<!-- id:N --> or <!-- id:N parent:P -->) are stripped from text.
    """
    id_match = _ID_COMMENT_RE.search(raw_text)
    item_id: Optional[int] = None
    parent_id: Optional[int] = None
    if id_match:
        item_id = int(id_match.group("id"))
        if id_match.group("parent"):
            parent_id = int(id_match.group("parent"))

    # Strip HTML comments and workstream annotations from text
    text = _HTML_COMMENT_RE.sub("", raw_text)
    text = _ANNOTATION_RE.sub("", text).strip()
    return text, item_id, parent_id


def parse_active_todos(content: str) -> ParsedTodos:
    """Parse ACTIVE TODOS.md content into open and done item lists.

    Pure function — no I/O or DB access.

    Priority assignment:
      - Lines under '## Urgent / This Week' → P3 (urgent default)
      - Lines under '## Active' subsections → P5 (active default)
      - Lines under '## Someday / Aspirational' → P8 (someday default)

    Workstream assignment:
      - Lines under '### <workstream>' subsection → workstream = <workstream>
      - Otherwise None

    Item text:
      - The trailing '*(source)*' annotation is stripped.
      - The '<!-- id:N -->' and '<!-- id:N parent:P -->' comments are extracted then stripped.

    Subtasks:
      - Lines with 2-space indent ("  - [ ] text") are subtasks.
      - parent_id is extracted from the <!-- parent:N --> comment.
    """
    result = ParsedTodos()
    if not content.strip():
        return result

    current_section: str = "active"       # default band
    current_workstream: Optional[str] = None
    current_parent_item_id: Optional[int] = None  # item_id of last top-level item seen

    for line in content.splitlines():
        # Detect top-level section (priority band)
        if _URGENT_HEADER_RE.search(line):
            current_section = "urgent"
            current_workstream = None
            current_parent_item_id = None
            continue
        if _ACTIVE_HEADER_RE.search(line):
            current_section = "active"
            current_workstream = None
            current_parent_item_id = None
            continue
        if _SOMEDAY_HEADER_RE.search(line):
            current_section = "someday"
            current_workstream = None
            current_parent_item_id = None
            continue

        # Detect workstream subsection (### name)
        ws_match = _WORKSTREAM_SECTION_RE.match(line)
        if ws_match:
            current_workstream = ws_match.group(1)
            current_parent_item_id = None
            continue

        # Try subtask (2-space indent) first
        sub_match = _SUBTASK_CHECKBOX_RE.match(line)
        if sub_match:
            checked = sub_match.group("checked").strip().lower() == "x"
            raw_text = sub_match.group("text")
            text, item_id, parent_id = _parse_item_text(raw_text)
            if not text:
                continue
            # If no <!-- parent:N --> comment, fall back to last top-level parent seen
            if parent_id is None:
                parent_id = current_parent_item_id
            item = ParsedItem(
                text=text,
                dedup_key=compute_dedup_key(text),
                priority=_priority_for_section(current_section),
                workstream=current_workstream,
                item_id=item_id,
                parent_id=parent_id,
            )
            if checked:
                result.done.append(item)
            else:
                result.open.append(item)
            continue

        # Parse top-level checkbox lines
        cb_match = _CHECKBOX_RE.match(line)
        if not cb_match:
            continue

        checked = cb_match.group("checked").strip().lower() == "x"
        raw_text = cb_match.group("text")
        text, item_id, _parent_id = _parse_item_text(raw_text)
        if not text:
            continue

        # Track this item's id for subtasks without an explicit <!-- parent:N --> comment
        current_parent_item_id = item_id

        item = ParsedItem(
            text=text,
            dedup_key=compute_dedup_key(text),
            priority=_priority_for_section(current_section),
            workstream=current_workstream,
            item_id=item_id,
            parent_id=None,  # top-level items never have a parent
        )

        if checked:
            result.done.append(item)
        else:
            result.open.append(item)

    return result


# ---------------------------------------------------------------------------
# DB sync (side effects isolated here)
# ---------------------------------------------------------------------------


def _update_priority(conn, item_id: int, new_priority: int) -> None:
    """Update the priority field for an existing item."""
    conn.execute(
        "UPDATE action_items SET priority = ? WHERE id = ?",
        (new_priority, item_id),
    )
    conn.commit()


def sync_obsidian_to_db(conn, content: str) -> SyncResult:
    """Apply ACTIVE TODOS.md edits to the DB.

    For each item in the file:
      - [x] done items: mark as done in DB if currently open (idempotent: skips if already done)
      - [ ] open items:
          - Not in DB → insert with source=OBSIDIAN_SOURCE
          - In DB + same priority band → no-op
          - In DB + different priority band → update priority
      - Open items that are already done in DB → left alone (DB is authoritative for done)

    Returns a SyncResult summarising what was changed.
    """
    result = SyncResult()
    parsed = parse_active_todos(content)

    # --- Process done items ---
    for item in parsed.done:
        existing = _find_any_status(conn, item.dedup_key)
        if existing is None:
            # Item was checked off but never existed in DB — skip
            continue
        if existing["status"] == ActionItemStatus.DONE:
            # Already done — idempotent skip
            continue
        mark_done(conn, existing["id"])
        result.done_count += 1
        log.info("Marked done: %r (id=%d)", item.text, existing["id"])

    # --- Process open items ---
    for item in parsed.open:
        existing = _find_any_status(conn, item.dedup_key)

        if existing is None:
            # New item — insert. For subtasks (item.parent_id is set), pass parent_id.
            # For existing items identified by item_id comment, skip insertion (handled below).
            row_id = insert_action_item(
                conn=conn,
                text=item.text,
                source=OBSIDIAN_SOURCE,
                source_message_id=None,
                priority=item.priority,
                parent_id=item.parent_id,
            )
            # Apply workstream if available
            if item.workstream:
                conn.execute(
                    "UPDATE action_items SET workstream = ? WHERE id = ?",
                    (item.workstream, row_id),
                )
                conn.commit()
            result.inserted_count += 1
            log.info("Inserted: %r (priority=%d, workstream=%s, parent_id=%s)", item.text, item.priority, item.workstream, item.parent_id)
            continue

        # Item exists — check if it's already done (leave it done, DB is authoritative)
        if existing["status"] == ActionItemStatus.DONE:
            result.skipped_already_done += 1
            log.debug("Skipping (already done in DB): %r", item.text)
            continue

        # Item exists and is open — check priority band
        if not _is_in_same_priority_band(existing["priority"], item.priority):
            _update_priority(conn, existing["id"], item.priority)
            result.priority_changed_count += 1
            log.info(
                "Priority updated: %r (DB=%d → file=%d)",
                item.text, existing["priority"], item.priority,
            )

    return result


def _find_any_status(conn, dedup_key: str) -> Optional[dict]:
    """Find an action_items row by dedup_key regardless of status.

    Returns a plain dict (id, status, priority) or None.
    Needed because find_duplicate() only returns open/snoozed items, but
    sync needs to handle already-done items too (to avoid re-marking).
    """
    cur = conn.execute(
        "SELECT id, status, priority FROM action_items WHERE dedup_key = ? LIMIT 1",
        (dedup_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "priority": row[2]}


# ---------------------------------------------------------------------------
# Render (pure generation — no DB writes)
# ---------------------------------------------------------------------------


def _render_item_line(item_id: int, text: str) -> str:
    """Render a top-level item line with id comment."""
    return f"- [ ] {text} <!-- id:{item_id} -->"


def _render_subtask_line(item_id: int, text: str, parent_id: int) -> str:
    """Render a subtask line with 2-space indent and id+parent comment."""
    return f"  - [ ] {text} <!-- id:{item_id} parent:{parent_id} -->"


def render_active_todos(conn) -> str:
    """Generate ACTIVE TODOS.md content from DB.

    Renders top-level items (parent_id IS NULL) with status IN ('open', 'snoozed'),
    ordered by priority, workstream, extracted_at. For each top-level item, renders
    its open subtasks (WHERE parent_id = ?) immediately after, indented 2 spaces.

    Items already done or dismissed are excluded. Subtask lines include
    '<!-- id:N parent:P -->' comments; parent lines include '<!-- id:N -->'.

    Returns the full markdown string. Same DB state always produces identical output.
    """
    cur = conn.execute(
        """
        SELECT id, text, priority, workstream
        FROM action_items
        WHERE parent_id IS NULL
          AND (status = 'open'
               OR (status = 'snoozed' AND snoozed_until < datetime('now')))
        ORDER BY priority ASC, workstream ASC, extracted_at ASC
        """,
    )
    rows = cur.fetchall()

    # Partition into bands
    urgent_items = []
    active_items: dict[str, list] = {}   # workstream → list of rows
    someday_items = []

    for row in rows:
        priority = row[2]
        workstream = row[3] or "general"
        if priority <= PRIORITY_URGENT_MAX:
            urgent_items.append(row)
        elif priority <= PRIORITY_ACTIVE_MAX:
            active_items.setdefault(workstream, []).append(row)
        else:
            someday_items.append(row)

    total = len(urgent_items) + sum(len(v) for v in active_items.values()) + len(someday_items)

    lines: list[str] = [
        "# ✅ ACTIVE TODOS",
        f"*Generated by LOS — {total} open items*",
        "",
    ]

    def _append_item_with_subtasks(row) -> None:
        """Append the parent item line followed by any open subtasks."""
        row_id = row[0]
        text = row[1]
        lines.append(_render_item_line(row_id, text))
        subtasks = get_subtasks(conn, row_id)
        for sub in subtasks:
            lines.append(_render_subtask_line(sub.id, sub.text, row_id))

    # --- Urgent section ---
    lines.append("## Urgent / This Week (P1–P3)")
    if urgent_items:
        for row in urgent_items:
            _append_item_with_subtasks(row)
    else:
        lines.append("*(none)*")
    lines.append("")

    # --- Active section ---
    lines.append("## Active (P4–P6)")
    lines.append("")
    if active_items:
        for workstream in sorted(active_items.keys()):
            lines.append(f"### {workstream}")
            for row in active_items[workstream]:
                _append_item_with_subtasks(row)
            lines.append("")
    else:
        lines.append("*(none)*")
        lines.append("")

    # --- Someday section ---
    lines.append("## Someday / Aspirational (P7–P9)")
    if someday_items:
        for row in someday_items:
            _append_item_with_subtasks(row)
    else:
        lines.append("*(none)*")
    lines.append("")
    lines.append("---")
    lines.append("*To mark done, dismiss, or snooze: tell Lobster via Telegram, or check the box in Obsidian.*")
    lines.append("*Next auto-sweep: nightly, ~02:30.*")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Vault git operations (side effects at the boundary)
# ---------------------------------------------------------------------------


def _git_pull(vault_path: Path) -> bool:
    """Pull latest changes from obsidian vault remote (if remote exists).

    Returns True if pull succeeded (or was skipped because no remote), False if
    the pull failed.  Callers should skip the commit/push cycle on False to
    avoid creating a diverged commit on top of a broken rebase state.
    """
    # Check if a remote exists before attempting pull
    result = subprocess.run(
        ["git", "remote"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        log.info("No git remote configured in vault — skipping pull")
        return True
    pull = subprocess.run(
        ["git", "pull", "--rebase", "--autostash"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if pull.returncode != 0:
        log.error(
            "git pull --rebase failed (skipping push for this cycle): %s",
            pull.stderr.strip(),
        )
        return False
    log.info("git pull: %s", pull.stdout.strip() or "up to date")
    return True


def _git_commit_and_push(vault_path: Path, todos_path: Path, timestamp: str) -> bool:
    """Stage ACTIVE TODOS.md, commit, and push.

    Returns True if a commit was made.
    """
    subprocess.run(
        ["git", "add", str(todos_path)],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
        check=True,
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        log.info("No changes to commit in vault")
        return False

    msg = f"todos: sync ACTIVE TODOS.md [{timestamp}]"
    commit = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        log.warning("git commit failed: %s", commit.stderr.strip())
        return False
    log.info("Committed: %s", msg)

    # Push only if a remote is configured
    remote_check = subprocess.run(
        ["git", "remote"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if not remote_check.stdout.strip():
        log.info("No remote — skipping push")
        return True

    push = subprocess.run(
        ["git", "push"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        log.warning("git push failed (non-fatal): %s", push.stderr.strip())
    else:
        log.info("Pushed to remote")

    return True


# ---------------------------------------------------------------------------
# Jobs.json enabled gate (Type B compliance — must gate before any DB work)
# ---------------------------------------------------------------------------


def _is_job_enabled(job_name: str) -> bool:
    """Return True if the job is enabled in jobs.json."""
    try:
        jobs_file = _get_workspace() / "scheduled-jobs" / "jobs.json"
        with jobs_file.open() as fh:
            data = json.load(fh)
        entry = data.get("jobs", {}).get(job_name, {})
        return bool(entry.get("enabled", True))
    except Exception:
        return True  # Safe default: enabled when unreadable


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Obsidian <-> DB bidirectional sync")
    parser.add_argument("--dry-run", action="store_true", help="Parse and sync to DB but skip vault write and commit")
    parser.add_argument("--vault", default=str(_VAULT_DEFAULT), help="Path to obsidian vault")
    parser.add_argument("--db", default=str(_DB_DEFAULT), help="Path to self_action_items.db")
    args = parser.parse_args()

    if not _is_job_enabled(JOB_NAME):
        log.info("Job '%s' is disabled in jobs.json — exiting", JOB_NAME)
        return

    vault_path = Path(args.vault)
    db_path = Path(args.db)
    todos_path = vault_path / ACTIVE_TODOS_FILENAME

    # Step 1: git pull vault (must succeed before we write anything)
    pull_ok = True
    if vault_path.exists() and (vault_path / ".git").exists():
        pull_ok = _git_pull(vault_path)
    else:
        log.warning("Vault directory not found or not a git repo: %s", vault_path)

    # Step 2: Parse ACTIVE TODOS.md
    if not todos_path.exists():
        log.info("ACTIVE TODOS.md not found at %s — nothing to parse", todos_path)
        content = ""
    else:
        content = todos_path.read_text(encoding="utf-8")
        log.info("Read %d bytes from %s", len(content), todos_path)

    # Step 3: Sync edits to DB
    conn = connect(db_path)
    try:
        if content:
            sync_result = sync_obsidian_to_db(conn, content)
            log.info("Sync complete: %s", sync_result)
        else:
            log.info("Empty content — skipping sync pass")
            sync_result = SyncResult()

        # Step 4: Regenerate ACTIVE TODOS.md from DB
        new_content = render_active_todos(conn)

        if args.dry_run:
            log.info("[dry-run] Would write %d chars to %s", len(new_content), todos_path)
            log.info("[dry-run] Preview (first 500 chars):\n%s", new_content[:500])
            return

        # Write the regenerated file
        todos_path.parent.mkdir(parents=True, exist_ok=True)
        todos_path.write_text(new_content, encoding="utf-8")
        log.info("Wrote regenerated ACTIVE TODOS.md (%d chars)", len(new_content))

        # Step 5: Commit and push (only when pull succeeded — avoids pushing
        # on top of a failed rebase and creating a diverged history)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if vault_path.exists() and (vault_path / ".git").exists():
            if not pull_ok:
                log.warning("Skipping commit/push because git pull failed")
            else:
                committed = _git_commit_and_push(vault_path, todos_path, timestamp)
                if committed:
                    log.info("Vault updated and committed")
                else:
                    log.info("No vault changes to commit")

    finally:
        conn.close()

    log.info(
        "todo-obsidian-sync complete: done=%d inserted=%d priority_changed=%d",
        sync_result.done_count,
        sync_result.inserted_count,
        sync_result.priority_changed_count,
    )


if __name__ == "__main__":
    main()
