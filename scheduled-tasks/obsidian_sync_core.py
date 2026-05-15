#!/usr/bin/env python3
"""
obsidian_sync_core.py — Shared sync/render/git logic for Obsidian vault integration.

Public API used by both todo_obsidian_sync.py and vault-processor.py:
  - parse_active_todos(content)       — pure parser; no I/O
  - sync_obsidian_to_db(conn, content) — syncs markdown edits to DB
  - apply_status_delta(file_content, conn) — update checkboxes in-place; preserve structure
  - render_active_todos(conn, last_synced=None) — generates markdown from DB (bootstrap only)
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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional
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
    mark_deleted,
    mark_done,
    get_item_by_id,
)
from src.utils.timezone import get_owner_zoneinfo as _get_owner_zoneinfo

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
    deleted_count: int = 0

    def __str__(self) -> str:
        return (
            f"done={self.done_count} inserted={self.inserted_count} "
            f"priority_changed={self.priority_changed_count} "
            f"skipped_already_done={self.skipped_already_done} "
            f"deleted={self.deleted_count}"
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

# Attribution sub-bullet pattern — exported for use by callers and future phases.
# Matches:  "  - [[...]]" (obsidian wiki-link) or "  - [telegram msg · ...]" etc.
# Note: the label-only form "[telegram msg]" (no middot, produced when created_at_iso
# is absent) is not matched here but is idempotent: re-rendering produces the same string.
ATTRIBUTION_LINE_RE = re.compile(r"^  - (?:\[\[.+\]\]|\[(?:telegram msg|voicenote|direct) ·)")

# ---------------------------------------------------------------------------
# Attribution (pure functions — no I/O or DB access)
# ---------------------------------------------------------------------------

# Source type labels used in archaeology-register attributions
_SOURCE_LABEL: dict[str, str] = {
    "telegram": "telegram msg",
    "voice": "voicenote",
    "direct": "direct",
}


def format_attribution(
    source: str,
    source_ref: Optional[str],
    source_section: Optional[str],
    created_at_iso: Optional[str],
) -> Optional[str]:
    """Return the attribution string for a todo item, or None if attribution is not applicable.

    Rules (Phase 1 — display pass from existing data):
    - source='obsidian': wiki-link form.
        - source_ref present: [[source_ref#source_section]] or [[source_ref]]
        - source_ref absent: omit attribution (no doc name to link)
    - source='telegram', 'voice', 'direct': archaeology register.
        - created_at_iso present: "[<label> · Mon May 11 · 10:35 AM PDT]"
        - created_at_iso absent or unparseable: "[<label>]" (label only, no timestamp)
    - source not in known set: None (skip attribution)

    This is a pure function — no I/O, no DB access.
    """
    if not source:
        return None

    if source.startswith("obsidian"):
        if not source_ref:
            return None
        # Strip the legacy "obsidian:ACTIVE TODOS.md" prefix if source is the obsidian source
        # constant — use source_ref as the actual doc name
        ref = source_ref
        # Remove common file extensions that Obsidian doesn't show in wiki-links
        if ref.endswith(".md"):
            ref = ref[:-3]
        if source_section:
            return f"[[{ref}#{source_section}]]"
        return f"[[{ref}]]"

    label = _SOURCE_LABEL.get(source)
    if label is None:
        return None

    if not created_at_iso:
        return f"[{label}]"

    try:
        dt_utc = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(_get_owner_zoneinfo())
    except (ValueError, AttributeError):
        return f"[{label}]"

    date_str = dt_local.strftime("%a %b %-d")
    time_str = dt_local.strftime("%-I:%M %p")
    tz_abbr = dt_local.strftime("%Z")
    return f"[{label} · {date_str} · {time_str} {tz_abbr}]"


def _render_attribution_line(attribution: str) -> str:
    """Wrap an attribution string as a 2-space-indented sub-bullet."""
    return f"  - {attribution}"


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

    After processing all file items:
      - DB items with status='open' whose dedup_key was NOT seen in this sync
        pass → mark as status='deleted' with deleted_at=now(). These were
        intentionally removed from the file without being checked off.

    Returns a SyncResult summarising what was changed.
    """
    result = SyncResult()
    parsed = parse_active_todos(content)

    # Collect dedup_keys of every item present in the file (open or done),
    # so we can detect DB open items that have silently disappeared.
    seen_dedup_keys: set[str] = set()

    # --- Process done items ---
    for item in parsed.done:
        seen_dedup_keys.add(item.dedup_key)
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
        seen_dedup_keys.add(item.dedup_key)
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

    # --- Mark deleted: open DB items not seen in this sync pass ---
    # Only applies to items from OBSIDIAN_SOURCE — items entered via other
    # channels (Telegram, etc.) are not subject to file-deletion detection.
    if seen_dedup_keys:
        placeholders = ",".join("?" * len(seen_dedup_keys))
        cur = conn.execute(
            f"""
            SELECT id, text, dedup_key FROM action_items
            WHERE status = 'open'
              AND source = ?
              AND dedup_key NOT IN ({placeholders})
            """,
            (OBSIDIAN_SOURCE, *seen_dedup_keys),
        )
        absent_rows = cur.fetchall()
        for row in absent_rows:
            mark_deleted(conn, row[0])
            result.deleted_count += 1
            log.info("Marked deleted (removed from file): %r (id=%d)", row[1], row[0])

    return result


# ---------------------------------------------------------------------------
# Delta-apply (structure-preserving update — only checkbox prefixes change)
# ---------------------------------------------------------------------------

# Marker blocks managed by Lobster at the bottom of the file
_LOBSTER_ADDITIONS_MARKER = "<!-- lobster-additions -->"
_LOBSTER_ADDITIONS_END = "<!-- /lobster-additions -->"

# Any checkbox line (0 or more leading spaces): "- [ ]" or "- [x]"
_ANY_CHECKBOX_RE = re.compile(r"^(\s*- \[)(?P<checked>[ xX])(\]\s+)(?P<text>.+)$")


def _extract_item_text_for_lookup(raw_text: str) -> str:
    """Strip HTML comments and workstream annotations from raw checkbox text.

    Mirrors the normalization in _parse_item_text so dedup_key matches.
    """
    text = HTML_COMMENT_RE.sub("", raw_text)
    text = ANNOTATION_RE.sub("", text).strip()
    return text


def apply_status_delta(file_content: str, conn) -> str:
    """Update checkbox prefixes in-place without rewriting surrounding structure.

    For each ``- [ ]`` or ``- [x]`` line in the file:
    - Compute dedup_key from item text
    - Look up DB status
    - If DB says done and file shows ``[ ]`` → flip to ``[x]``
    - If DB says open/snoozed and file shows ``[x]`` → flip to ``[ ]``
    - All other lines (headers, subbullets, blank lines, non-todo lines) are
      passed through unchanged.

    Items in the DB that are open/snoozed but absent from the file (added via
    Telegram or other non-obsidian sources) are appended at the bottom inside
    a ``<!-- lobster-additions -->`` block so they don't disrupt Dan's structure.

    The ``<!-- done since last reset -->`` section previously managed by
    render_active_todos is not reproduced here — done items stay as ``[x]`` in
    place until the 5am daily reset job removes them.

    Returns the (possibly identical) updated file content string.
    """
    lines = file_content.splitlines(keepends=True)

    # --- Pass 1: collect dedup_keys present in the *main body* of the file ---
    # We deliberately exclude items inside the <!-- lobster-additions --> block.
    #
    # Why: Pass 3 rebuilds the additions block from scratch whenever new DB items
    # arrive.  If additions-block items were included in file_dedup_keys they would
    # be excluded from items_to_append, then disappear when the block is stripped
    # and rewritten — causing the oscillation bug where items alternate between
    # "present" and "absent" on consecutive runs.
    #
    # Using only the main-body keys means additions-block items are always eligible
    # for re-inclusion in the rebuilt block, making the output stable across runs.
    file_dedup_keys: set[str] = set()
    _in_additions = False
    for raw_line in lines:
        stripped_line = raw_line.rstrip("\n")
        if stripped_line.strip() == _LOBSTER_ADDITIONS_MARKER:
            _in_additions = True
            continue
        if stripped_line.strip() == _LOBSTER_ADDITIONS_END:
            _in_additions = False
            continue
        if _in_additions:
            continue  # Skip lines inside the additions block

        line = stripped_line
        m = _ANY_CHECKBOX_RE.match(line)
        if not m:
            continue
        raw_text = m.group("text")
        item_text = _extract_item_text_for_lookup(raw_text)
        if not item_text:
            continue
        # Skip the DISABLE PROCESSING guard
        if "DISABLE PROCESSING" in item_text.upper():
            continue
        file_dedup_keys.add(compute_dedup_key(item_text))

    # --- Build DB lookup: dedup_key → (id, status) ---
    # Only fetch open/snoozed/done — deleted/dismissed items are not relevant.
    db_by_key: dict[str, tuple[int, str]] = {}
    cur = conn.execute(
        """
        SELECT id, dedup_key, status
        FROM action_items
        WHERE status IN ('open', 'snoozed', 'done')
        """,
    )
    for row in cur.fetchall():
        db_by_key[row[1]] = (row[0], row[2])

    # --- Pass 2: rewrite checkbox prefixes in-place ---
    out_lines: list[str] = []
    in_additions_block = False

    for raw_line in lines:
        # Track whether we are inside the existing additions block
        stripped = raw_line.rstrip("\n")
        if stripped.strip() == _LOBSTER_ADDITIONS_MARKER:
            in_additions_block = True
            out_lines.append(raw_line)
            continue
        if stripped.strip() == _LOBSTER_ADDITIONS_END:
            in_additions_block = False
            out_lines.append(raw_line)
            continue

        # Lines inside the additions block get checkbox treatment too
        m = _ANY_CHECKBOX_RE.match(stripped)
        if not m:
            out_lines.append(raw_line)
            continue

        raw_text = m.group("text")
        item_text = _extract_item_text_for_lookup(raw_text)

        # Skip the DISABLE PROCESSING guard — never touch it
        if "DISABLE PROCESSING" in item_text.upper():
            out_lines.append(raw_line)
            continue

        if not item_text:
            out_lines.append(raw_line)
            continue

        dedup_key = compute_dedup_key(item_text)
        current_checked = m.group("checked").strip().lower() == "x"
        db_entry = db_by_key.get(dedup_key)

        if db_entry is None:
            # Not in DB — leave line as-is (may be a new item Dan just added)
            out_lines.append(raw_line)
            continue

        _db_id, db_status = db_entry
        db_done = db_status == ActionItemStatus.DONE

        if db_done and not current_checked:
            # DB says done but file shows open — flip to [x]
            new_line = m.group(1) + "x" + m.group(3) + raw_text
            eol = "\n" if raw_line.endswith("\n") else ""
            out_lines.append(new_line + eol)
            log.debug("Delta: marked [x] in file: %r", item_text[:60])
        elif not db_done and current_checked:
            # DB says open/snoozed but file shows done — flip back to [ ]
            new_line = m.group(1) + " " + m.group(3) + raw_text
            eol = "\n" if raw_line.endswith("\n") else ""
            out_lines.append(new_line + eol)
            log.debug("Delta: unmarked [ ] in file: %r", item_text[:60])
        else:
            # Already in sync — pass through unchanged
            out_lines.append(raw_line)

    # --- Pass 3: find open/snoozed items that are NOT yet in the file ---
    # These are items added via Telegram (source != obsidian) that need to
    # appear in the file so Dan can see and manage them.

    # Detect optional Phase 2+ columns for attribution
    _delta_cols = {row[1] for row in conn.execute("PRAGMA table_info(action_items)").fetchall()}
    _delta_source_ref = "source_ref" if "source_ref" in _delta_cols else "NULL AS source_ref"
    _delta_source_section = "source_section" if "source_section" in _delta_cols else "NULL AS source_section"

    # items_to_append: (id, text, priority, source, extracted_at, source_ref, source_section)
    items_to_append: list[tuple] = []
    if file_dedup_keys:
        placeholders = ",".join("?" * len(file_dedup_keys))
        cur2 = conn.execute(
            f"""
            SELECT id, text, priority, source, extracted_at,
                   {_delta_source_ref}, {_delta_source_section}
            FROM action_items
            WHERE status IN ('open', 'snoozed')
              AND parent_id IS NULL
              AND dedup_key NOT IN ({placeholders})
            ORDER BY priority ASC, extracted_at ASC
            """,
            tuple(file_dedup_keys),
        )
    else:
        # No items in file at all — append everything open/snoozed
        cur2 = conn.execute(
            f"""
            SELECT id, text, priority, source, extracted_at,
                   {_delta_source_ref}, {_delta_source_section}
            FROM action_items
            WHERE status IN ('open', 'snoozed')
              AND parent_id IS NULL
            ORDER BY priority ASC, extracted_at ASC
            """,
        )
    items_to_append = list(cur2.fetchall())

    if not items_to_append:
        return "".join(out_lines)

    # Strip existing additions block from the tail (we'll rewrite it)
    # Find the last occurrence of _LOBSTER_ADDITIONS_MARKER in out_lines
    tail_start: Optional[int] = None
    for idx in range(len(out_lines) - 1, -1, -1):
        if out_lines[idx].strip() == _LOBSTER_ADDITIONS_MARKER:
            tail_start = idx
            break

    if tail_start is not None:
        out_lines = out_lines[:tail_start]

    # Ensure file ends with a newline before appending
    if out_lines and not out_lines[-1].endswith("\n"):
        out_lines[-1] = out_lines[-1] + "\n"
    if not out_lines or out_lines[-1].strip() != "":
        out_lines.append("\n")

    # Append new items block
    out_lines.append(_LOBSTER_ADDITIONS_MARKER + "\n")
    out_lines.append("*Items added via Telegram — move or edit freely:*\n")
    out_lines.append("\n")
    for row in items_to_append:
        item_id, item_text = row[0], row[1]
        item_source = row[3] if len(row) > 3 else None
        item_extracted_at = row[4] if len(row) > 4 else None
        item_source_ref = row[5] if len(row) > 5 else None
        item_source_section = row[6] if len(row) > 6 else None

        out_lines.append(f"- [ ] {item_text} <!-- id:{item_id} -->\n")
        attribution = format_attribution(
            item_source or "", item_source_ref, item_source_section, item_extracted_at
        )
        if attribution:
            out_lines.append(_render_attribution_line(attribution) + "\n")
        log.info("Delta: appended new item from DB: %r (id=%d)", item_text[:60], item_id)
    out_lines.append(_LOBSTER_ADDITIONS_END + "\n")

    return "".join(out_lines)


# ---------------------------------------------------------------------------
# Render (pure generation — no DB writes; retained for bootstrap / fresh installs)
# ---------------------------------------------------------------------------


def _render_item_line(item_id: int, text: str) -> str:
    return f"- [ ] {text} <!-- id:{item_id} -->"


def _render_subtask_line(item_id: int, text: str, parent_id: int) -> str:
    return f"  - [ ] {text} <!-- id:{item_id} parent:{parent_id} -->"


def _done_since_cutoff_utc(done_reset_hour_pst: int = 5) -> datetime:
    """Return the most recent daily cutoff as a UTC datetime.

    The cutoff is ``done_reset_hour_pst`` hours in Pacific Time.  Because PST
    is UTC-8 and PDT is UTC-7, the conservative UTC equivalent is:

        hour_utc = done_reset_hour_pst + 8   (PST — the later of the two)

    This means the window stays open slightly longer during PDT, which is the
    safer direction (items persist a bit longer rather than disappearing early).

    Algorithm:
    1. Compute today's cutoff at ``done_reset_hour_pst + 8`` UTC.
    2. If now < cutoff (we haven't reached today's cutoff yet), use yesterday's.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_hour_utc = done_reset_hour_pst + 8  # PST offset; safe for PDT too
    today_cutoff = now_utc.replace(
        hour=cutoff_hour_utc, minute=0, second=0, microsecond=0
    )
    if now_utc < today_cutoff:
        # We haven't passed today's cutoff — roll back to yesterday's
        from datetime import timedelta
        today_cutoff -= timedelta(days=1)
    return today_cutoff


def render_active_todos(
    conn,
    last_synced: Optional[str] = None,
    done_reset_hour_pst: int = 5,
) -> str:
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
    done_reset_hour_pst:
        Hour in Pacific Standard Time (default 5, i.e. 5 AM PST = 13:00 UTC)
        after which done items from the previous window stop appearing in the
        file.  Done items completed since the most recent cutoff are rendered
        as ``- [x]`` in a separate section below all open items.

    Returns the full markdown string. Same DB state + same last_synced +
    same done_reset_hour_pst always produces identical output (pure w.r.t.
    inputs, assuming now() is held constant).

    Behavior note (PR #1131): This function always inserts the
    "- [ ] 🔒 DISABLE PROCESSING" guard line near the top of the rendered
    output, regardless of whether last_synced is provided.  This is intentional:
    vault-processor.py requires the guard line to be present to proceed (Section 4
    of design.md).  As a result, todo_obsidian_sync.py now writes this guard line
    on every 30-minute sync run — a deliberate bootstrap behavior, not a silent
    change.  The guard line does not affect todo_obsidian_sync.py's own logic
    (it does not read or check the guard).
    """
    # Detect optional Phase 2+ columns (source_ref, source_section).
    # In Phase 1 these don't exist; falling back to NULL keeps attribution
    # working without schema changes.
    _cols = {row[1] for row in conn.execute("PRAGMA table_info(action_items)").fetchall()}
    _source_ref_expr = "source_ref" if "source_ref" in _cols else "NULL AS source_ref"
    _source_section_expr = "source_section" if "source_section" in _cols else "NULL AS source_section"

    cur = conn.execute(
        f"""
        SELECT id, text, priority, workstream, source, extracted_at,
               {_source_ref_expr}, {_source_section_expr}
        FROM action_items
        WHERE parent_id IS NULL
          AND (status = 'open'
               OR (status = 'snoozed' AND snoozed_until < datetime('now')))
        ORDER BY priority ASC, workstream ASC, extracted_at ASC
        """,
    )
    rows = cur.fetchall()

    # Fetch done items completed since the most recent daily cutoff
    cutoff_utc = _done_since_cutoff_utc(done_reset_hour_pst)
    cutoff_iso = cutoff_utc.isoformat()
    cur_done = conn.execute(
        """
        SELECT id, text
        FROM action_items
        WHERE parent_id IS NULL
          AND status = 'done'
          AND done_at >= ?
        ORDER BY done_at ASC
        """,
        (cutoff_iso,),
    )
    recently_done_rows = cur_done.fetchall()

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
        # row indices: 0=id, 1=text, 2=priority, 3=workstream,
        #              4=source, 5=extracted_at, 6=source_ref, 7=source_section
        source = row[4] if len(row) > 4 else None
        extracted_at = row[5] if len(row) > 5 else None
        source_ref = row[6] if len(row) > 6 else None
        source_section = row[7] if len(row) > 7 else None

        lines.append(_render_item_line(row_id, text))

        # Attribution sub-bullet: only for top-level items with a source set
        attribution = format_attribution(source or "", source_ref, source_section, extracted_at)
        if attribution:
            lines.append(_render_attribution_line(attribution))

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

    # --- Recently done section (visible until next daily reset) ---
    if recently_done_rows:
        lines.append("---")
        lines.append("<!-- done since last reset -->")
        for row in recently_done_rows:
            lines.append(f"- [x] {row[1]} <!-- id:{row[0]} -->")
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
