#!/usr/bin/env python3
"""
obsidian_sync_core.py — Shared sync/render/git logic for Obsidian vault integration.

Public API used by both todo_obsidian_sync.py and vault-processor.py:
  - parse_active_todos(content)       — pure parser; no I/O
  - sync_obsidian_to_db(conn, content) — syncs markdown edits to DB
  - render_active_todos(conn, last_synced=None) — generates markdown from DB
  - git_pull(vault_path)              — git pull --rebase --autostash
  - git_commit_and_push(vault_path, files, message) — stage, commit, push
  - acquire_lock_or_skip(lock_path)   — cross-process fcntl.flock (non-blocking)
  - release_lock(lock_fd)             — release previously acquired lock

All functions here are public (no leading underscore). Callers may import any
of these directly without worrying about API stability breakage — this module
is the stable boundary.
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional
import re
import sys

# ---------------------------------------------------------------------------
# Path setup (allow import from scheduled-tasks/ without installing)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import (
    ActionItemStatus,
    compute_dedup_key,
    connect,
    get_subtasks,
    insert_action_item,
    mark_done,
    get_item_by_id,
)

log = logging.getLogger("obsidian-sync-core")

# ---------------------------------------------------------------------------
# Constants (named after spec requirements)
# ---------------------------------------------------------------------------

ACTIVE_TODOS_FILENAME = "✅ ACTIVE TODOS.md"
OBSIDIAN_SOURCE = "obsidian:ACTIVE TODOS.md"

# Priority band boundaries (Section 2 & 6 of design)
PRIORITY_URGENT_MAX = 3    # P1–P3: Urgent / This Week
PRIORITY_ACTIVE_MAX = 6    # P4–P6: Active
# P7–P9: Someday / Aspirational (anything > PRIORITY_ACTIVE_MAX)

# Representative midpoints for new items
PRIORITY_URGENT_DEFAULT = 3
PRIORITY_ACTIVE_DEFAULT = 5
PRIORITY_SOMEDAY_DEFAULT = 8

# Footer text — legacy (no last_synced)
FOOTER_LEGACY = "*Next auto-sweep: nightly, ~02:30.*"
# Footer template — vault-watcher (has last_synced timestamp)
FOOTER_SYNCED_TEMPLATE = "*Last synced: {last_synced}. Next sync on push.*"

# ---------------------------------------------------------------------------
# Data structures (immutable / pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedItem:
    """A single TODO item parsed from ACTIVE TODOS.md."""
    text: str
    dedup_key: str
    priority: int
    workstream: Optional[str]
    item_id: Optional[int] = None
    parent_id: Optional[int] = None


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
# Regex constants (shared with callers to avoid drift)
# ---------------------------------------------------------------------------

# Section header patterns used to assign priority bands
URGENT_HEADER_RE = re.compile(r"##\s+Urgent\s*/\s*This Week", re.IGNORECASE)
ACTIVE_HEADER_RE = re.compile(r"##\s+Active\s*\(P4", re.IGNORECASE)
SOMEDAY_HEADER_RE = re.compile(r"##\s+Someday\s*/\s*Aspirational", re.IGNORECASE)

# Workstream subsection: ### <name>
WORKSTREAM_SECTION_RE = re.compile(r"^###\s+(\S+)", re.MULTILINE)

# Top-level checkbox line: "- [ ] text" or "- [x] text" (no leading spaces)
CHECKBOX_RE = re.compile(r"^- \[(?P<checked>[ xX])\]\s+(?P<text>.+)$")

# Subtask checkbox line: "  - [ ] text" or "  - [x] text" (exactly 2-space indent)
SUBTASK_CHECKBOX_RE = re.compile(r"^  - \[(?P<checked>[ xX])\]\s+(?P<text>.+)$")

# Trailing workstream annotation: "  *(workstream)*" at end of line
ANNOTATION_RE = re.compile(r"\s+\*\([^)]+\)\*\s*$")

# HTML comment: <!-- id:N --> or <!-- id:N parent:P -->
ID_COMMENT_RE = re.compile(r"<!--\s*id:(?P<id>\d+)(?:\s+parent:(?P<parent>\d+))?\s*-->")

# Strip HTML comment from text
HTML_COMMENT_RE = re.compile(r"\s*<!--[^>]*-->")


# ---------------------------------------------------------------------------
# Parsing (pure functions — no I/O or DB access)
# ---------------------------------------------------------------------------


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
    id_match = ID_COMMENT_RE.search(raw_text)
    item_id: Optional[int] = None
    parent_id: Optional[int] = None
    if id_match:
        item_id = int(id_match.group("id"))
        if id_match.group("parent"):
            parent_id = int(id_match.group("parent"))

    # Strip HTML comments and workstream annotations from text
    text = HTML_COMMENT_RE.sub("", raw_text)
    text = ANNOTATION_RE.sub("", text).strip()
    return text, item_id, parent_id


def parse_active_todos(content: str) -> ParsedTodos:
    """Parse ACTIVE TODOS.md content into open and done item lists.

    Pure function — no I/O or DB access.
    """
    result = ParsedTodos()
    if not content.strip():
        return result

    current_section: str = "active"
    current_workstream: Optional[str] = None
    current_parent_item_id: Optional[int] = None

    for line in content.splitlines():
        # Detect top-level section (priority band)
        if URGENT_HEADER_RE.search(line):
            current_section = "urgent"
            current_workstream = None
            current_parent_item_id = None
            continue
        if ACTIVE_HEADER_RE.search(line):
            current_section = "active"
            current_workstream = None
            current_parent_item_id = None
            continue
        if SOMEDAY_HEADER_RE.search(line):
            current_section = "someday"
            current_workstream = None
            current_parent_item_id = None
            continue

        # Detect workstream subsection (### name)
        ws_match = WORKSTREAM_SECTION_RE.match(line)
        if ws_match:
            current_workstream = ws_match.group(1)
            current_parent_item_id = None
            continue

        # Try subtask (2-space indent) first
        sub_match = SUBTASK_CHECKBOX_RE.match(line)
        if sub_match:
            checked = sub_match.group("checked").strip().lower() == "x"
            raw_text = sub_match.group("text")
            text, item_id, parent_id = _parse_item_text(raw_text)
            if not text:
                continue
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
        cb_match = CHECKBOX_RE.match(line)
        if not cb_match:
            continue

        checked = cb_match.group("checked").strip().lower() == "x"
        raw_text = cb_match.group("text")
        text, item_id, _parent_id = _parse_item_text(raw_text)
        if not text:
            continue

        current_parent_item_id = item_id

        item = ParsedItem(
            text=text,
            dedup_key=compute_dedup_key(text),
            priority=_priority_for_section(current_section),
            workstream=current_workstream,
            item_id=item_id,
            parent_id=None,
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


def _find_any_status(conn, dedup_key: str) -> Optional[dict]:
    """Find an action_items row by dedup_key regardless of status."""
    cur = conn.execute(
        "SELECT id, status, priority FROM action_items WHERE dedup_key = ? LIMIT 1",
        (dedup_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "priority": row[2]}


def sync_obsidian_to_db(conn, content: str) -> SyncResult:
    """Apply ACTIVE TODOS.md edits to the DB.

    For each item in the file:
      - [x] done items: mark as done in DB if currently open (idempotent)
      - [ ] open items:
          - Not in DB → insert with source=OBSIDIAN_SOURCE
          - In DB + same priority band → no-op
          - In DB + different priority band → update priority
      - Open items that are already done in DB → left alone (DB is authoritative)

    Returns a SyncResult summarising what was changed.
    """
    result = SyncResult()
    parsed = parse_active_todos(content)

    # --- Process done items ---
    for item in parsed.done:
        existing = _find_any_status(conn, item.dedup_key)
        if existing is None:
            continue
        if existing["status"] == ActionItemStatus.DONE:
            continue
        mark_done(conn, existing["id"])
        result.done_count += 1
        log.info("Marked done: %r (id=%d)", item.text, existing["id"])

    # --- Process open items ---
    for item in parsed.open:
        existing = _find_any_status(conn, item.dedup_key)

        if existing is None:
            row_id = insert_action_item(
                conn=conn,
                text=item.text,
                source=OBSIDIAN_SOURCE,
                source_message_id=None,
                priority=item.priority,
                parent_id=item.parent_id,
            )
            if item.workstream:
                conn.execute(
                    "UPDATE action_items SET workstream = ? WHERE id = ?",
                    (item.workstream, row_id),
                )
                conn.commit()
            result.inserted_count += 1
            log.info(
                "Inserted: %r (priority=%d, workstream=%s, parent_id=%s)",
                item.text, item.priority, item.workstream, item.parent_id,
            )
            continue

        if existing["status"] == ActionItemStatus.DONE:
            result.skipped_already_done += 1
            log.debug("Skipping (already done in DB): %r", item.text)
            continue

        if not _is_in_same_priority_band(existing["priority"], item.priority):
            _update_priority(conn, existing["id"], item.priority)
            result.priority_changed_count += 1
            log.info(
                "Priority updated: %r (DB=%d → file=%d)",
                item.text, existing["priority"], item.priority,
            )

    return result


# ---------------------------------------------------------------------------
# Render (pure generation — no DB writes)
# ---------------------------------------------------------------------------


def _render_item_line(item_id: int, text: str) -> str:
    return f"- [ ] {text} <!-- id:{item_id} -->"


def _render_subtask_line(item_id: int, text: str, parent_id: int) -> str:
    return f"  - [ ] {text} <!-- id:{item_id} parent:{parent_id} -->"


def render_active_todos(conn, last_synced: Optional[str] = None) -> str:
    """Generate ACTIVE TODOS.md content from DB.

    Parameters
    ----------
    conn:
        Open sqlite3 connection to self_action_items.db.
    last_synced:
        When provided (vault-processor.py path), the footer shows
        "Last synced: <timestamp>. Next sync on push."
        When absent (legacy todo_obsidian_sync.py path), the footer retains
        "Next auto-sweep: nightly, ~02:30." for backward compatibility.

    Returns the full markdown string. Same DB state + same last_synced always
    produces identical output (pure w.r.t. inputs).

    Behavior note (PR #1131): This function always inserts the
    "- [ ] 🔒 DISABLE PROCESSING" guard line near the top of the rendered
    output, regardless of whether last_synced is provided.  This is intentional:
    vault-processor.py requires the guard line to be present to proceed (Section 4
    of design.md).  As a result, todo_obsidian_sync.py now writes this guard line
    on every 30-minute sync run — a deliberate bootstrap behavior, not a silent
    change.  The guard line does not affect todo_obsidian_sync.py's own logic
    (it does not read or check the guard).
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

    urgent_items = []
    active_items: dict[str, list] = {}
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

    # The DISABLE PROCESSING guard line is always rendered here — even on the
    # todo_obsidian_sync.py (legacy) path where last_synced is None.
    #
    # Intentional behaviour: vault-processor.py requires this guard to be present
    # and unchecked before it will process the vault (Section 4 of design.md).
    # Rendering it unconditionally ensures the invariant is established on the
    # first sync run, regardless of which caller triggered the render.  This is a
    # deliberate migration bootstrap: after the first todo_obsidian_sync.py run
    # following PR #1131 deployment, the guard line will appear in ACTIVE TODOS.md
    # and vault-processor.py will proceed normally on subsequent watcher-triggered
    # runs.  If the guard is removed manually, vault-processor.py will alert Dan
    # and skip (State 3).
    lines: list[str] = [
        "# ✅ ACTIVE TODOS",
        f"*Generated by LOS — {total} open items*",
        "",
        "- [ ] 🔒 DISABLE PROCESSING",
        "",
    ]

    def _append_item_with_subtasks(row) -> None:
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

    if last_synced is not None:
        lines.append(FOOTER_SYNCED_TEMPLATE.format(last_synced=last_synced))
    else:
        lines.append(FOOTER_LEGACY)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Vault git operations (side effects at the boundary)
# ---------------------------------------------------------------------------


def git_pull(vault_path: Path) -> bool:
    """Pull latest changes from obsidian vault remote (if remote exists).

    Returns True if pull succeeded (or was skipped — no remote), False on failure.
    Callers should skip the commit/push cycle on False to avoid pushing on top
    of a failed rebase state.
    """
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
        # Abort any partial rebase
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(vault_path),
            capture_output=True,
            text=True,
        )
        return False
    log.info("git pull: %s", pull.stdout.strip() or "up to date")
    return True


def git_commit_and_push(vault_path: Path, files: list[Path], message: str) -> bool:
    """Stage the given files, commit, and push.

    Returns True if a commit was made (or attempted), False if nothing to commit.
    Push failure is non-fatal — logs warning and returns True.

    Parameters
    ----------
    vault_path:
        Root of the obsidian-vault git repo.
    files:
        List of absolute or vault-relative paths to stage.
    message:
        Commit message.
    """
    for f in files:
        subprocess.run(
            ["git", "add", str(f)],
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

    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        # "nothing to commit" is treated as success
        if "nothing to commit" in commit.stdout.lower():
            log.info("git commit: nothing to commit")
            return False
        log.warning("git commit failed: %s", commit.stderr.strip())
        return False
    log.info("Committed: %s", message)

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
# Process mutex (cross-process fcntl.flock)
# ---------------------------------------------------------------------------


def acquire_lock_or_skip(lock_path: Path) -> Optional[IO]:
    """Acquire the vault-processor lockfile in non-blocking mode.

    Returns an open file object (the lock fd) on success, or None if the lock
    is already held (processor is running — caller should skip and exit 0).

    Usage:
        lock_fd = acquire_lock_or_skip(lock_path)
        if lock_fd is None:
            log.info("skipping: processor already running")
            return
        try:
            # ... do work ...
        finally:
            release_lock(lock_fd)
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except BlockingIOError:
        lock_fd.close()
        return None


def release_lock(lock_fd: IO) -> None:
    """Release and close a lock file descriptor acquired by acquire_lock_or_skip."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except Exception:
        pass  # Lock already released (e.g., on process exit)
