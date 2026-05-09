"""
LOS — Action Items Database

Schema, connection, and access layer for self_action_items.db.

Design principles:
- Pure functions where possible — take conn as argument, return values
- Side effects (DB writes) are explicit and at the boundary
- No global state — callers own the connection lifecycle

DB location: ~/lobster-user-config/data/self_action_items.db
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Status enum — single source of truth for the status domain
# ---------------------------------------------------------------------------


class ActionItemStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_USER_CONFIG_DIR = Path.home() / "lobster-user-config"
DEFAULT_DB_PATH = _USER_CONFIG_DIR / "data" / "self_action_items.db"

# ---------------------------------------------------------------------------
# Schema — exactly as specified in the task prompt
# ---------------------------------------------------------------------------

DB_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS action_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    text             TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_message_id TEXT,
    extracted_at     TEXT NOT NULL,
    priority         INTEGER DEFAULT 5,
    mention_count    INTEGER DEFAULT 1,
    status           TEXT DEFAULT '{ActionItemStatus.OPEN}',
    snoozed_until    TEXT,
    done_at          TEXT,
    dismissed_at     TEXT,
    notes            TEXT,
    dedup_key        TEXT
);

CREATE INDEX IF NOT EXISTS idx_action_items_status
    ON action_items(status);

CREATE INDEX IF NOT EXISTS idx_action_items_extracted_at
    ON action_items(extracted_at);

CREATE INDEX IF NOT EXISTS idx_action_items_dedup_key
    ON action_items(dedup_key);
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionItem:
    id: int
    text: str
    source: str
    source_message_id: Optional[str]
    extracted_at: str
    priority: int
    mention_count: int
    status: str
    snoozed_until: Optional[str]
    done_at: Optional[str]
    dismissed_at: Optional[str]
    notes: Optional[str]
    dedup_key: Optional[str]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (or create) the action items SQLite DB and apply the schema.

    Creates parent directories if needed. Returns an open connection.
    Callers are responsible for closing the connection.
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA_SQL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_dedup_key(text: str) -> str:
    """Produce a 16-char hex dedup key from normalized text.

    Normalization: lowercase, strip non-alphanumeric (except spaces),
    collapse whitespace.
    """
    normalized = re.sub(r"[^a-z0-9 ]", "", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _row_to_item(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        id=row["id"],
        text=row["text"],
        source=row["source"],
        source_message_id=row["source_message_id"],
        extracted_at=row["extracted_at"],
        priority=row["priority"],
        mention_count=row["mention_count"],
        status=row["status"],
        snoozed_until=row["snoozed_until"],
        done_at=row["done_at"],
        dismissed_at=row["dismissed_at"],
        notes=row["notes"],
        dedup_key=row["dedup_key"],
    )


# ---------------------------------------------------------------------------
# Writes (side-effectful, at the boundary)
# ---------------------------------------------------------------------------


def insert_action_item(
    conn: sqlite3.Connection,
    text: str,
    source: str,
    source_message_id: Optional[str],
    priority: int = 5,
    notes: Optional[str] = None,
) -> int:
    """Insert a new action item. Returns the row id."""
    dedup_key = compute_dedup_key(text)
    extracted_at = _now_iso()
    cursor = conn.execute(
        """
        INSERT INTO action_items
            (text, source, source_message_id, extracted_at, priority, status,
             mention_count, dedup_key, notes)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (text, source, source_message_id, extracted_at, priority, ActionItemStatus.OPEN, dedup_key, notes),
    )
    conn.commit()
    return cursor.lastrowid


def mark_done(conn: sqlite3.Connection, item_id: int) -> None:
    """Set status='done' and record done_at timestamp."""
    conn.execute(
        "UPDATE action_items SET status=?, done_at=? WHERE id=?",
        (ActionItemStatus.DONE, _now_iso(), item_id),
    )
    conn.commit()


def mark_dismissed(conn: sqlite3.Connection, item_id: int) -> None:
    """Set status='dismissed' and record dismissed_at.

    Dismissed items are NOT deleted — they remain reviewable in weekly review.
    """
    conn.execute(
        "UPDATE action_items SET status=?, dismissed_at=? WHERE id=?",
        (ActionItemStatus.DISMISSED, _now_iso(), item_id),
    )
    conn.commit()


def mark_snoozed(conn: sqlite3.Connection, item_id: int, until_date: str) -> None:
    """Set status='snoozed' with a custom date (YYYY-MM-DD format)."""
    conn.execute(
        "UPDATE action_items SET status=?, snoozed_until=? WHERE id=?",
        (ActionItemStatus.SNOOZED, until_date, item_id),
    )
    conn.commit()


def increment_mention_count(conn: sqlite3.Connection, item_id: int) -> None:
    """Increment mention_count for an existing item by 1."""
    conn.execute(
        "UPDATE action_items SET mention_count = mention_count + 1 WHERE id=?",
        (item_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Reads (pure queries — no side effects)
# ---------------------------------------------------------------------------


def get_item_by_id(conn: sqlite3.Connection, item_id: int) -> Optional[ActionItem]:
    """Return a single ActionItem by primary key, or None if not found."""
    cursor = conn.execute(
        "SELECT * FROM action_items WHERE id=?", (item_id,)
    )
    row = cursor.fetchone()
    return _row_to_item(row) if row else None


def get_open_items(
    conn: sqlite3.Connection,
    limit: int = 20,
) -> list[ActionItem]:
    """Return open (non-snoozed-to-future) items, sorted by priority ASC then mention_count DESC.

    Snoozed items whose snoozed_until is in the past are treated as open again.
    """
    cursor = conn.execute(
        """
        SELECT * FROM action_items
        WHERE status = ?
          OR (status = ? AND snoozed_until < datetime('now'))
        ORDER BY priority ASC, mention_count DESC, extracted_at ASC
        LIMIT ?
        """,
        (ActionItemStatus.OPEN, ActionItemStatus.SNOOZED, limit),
    )
    return [_row_to_item(row) for row in cursor.fetchall()]


def get_dismissed_items_since(
    conn: sqlite3.Connection,
    since_iso: str,
) -> list[ActionItem]:
    """Return dismissed items since a given ISO timestamp (for weekly review)."""
    cursor = conn.execute(
        """
        SELECT * FROM action_items
        WHERE status = ?
          AND dismissed_at >= ?
        ORDER BY dismissed_at DESC
        """,
        (ActionItemStatus.DISMISSED, since_iso),
    )
    return [_row_to_item(row) for row in cursor.fetchall()]


def find_duplicate(
    conn: sqlite3.Connection,
    text: str,
) -> Optional[ActionItem]:
    """Find an existing open or snoozed item with the same normalized text.

    Returns the matching ActionItem, or None if no duplicate exists.
    Dismissed items are excluded — they are archived, not active.
    """
    key = compute_dedup_key(text)
    cursor = conn.execute(
        """
        SELECT * FROM action_items
        WHERE dedup_key = ?
          AND status IN (?, ?)
        LIMIT 1
        """,
        (key, ActionItemStatus.OPEN, ActionItemStatus.SNOOZED),
    )
    row = cursor.fetchone()
    return _row_to_item(row) if row else None
