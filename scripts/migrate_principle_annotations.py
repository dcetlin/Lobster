#!/usr/bin/env python3
"""
scripts/migrate_principle_annotations.py — Add principle_annotation column to events table

Adds an optional TEXT column to the events table in memory.db that stores a JSON blob
recording when a dispatcher decision diverged from the smooth default because a principle
was actively constraining the output.

Schema for the JSON blob:
    {
        "principle": "attunement_over_assumption",
        "divergence": "brief description of what smooth default was resisted",
        "confidence": "high|medium|low"
    }

Migration is idempotent: safe to run multiple times. The column is only added if it does
not already exist (detected via PRAGMA table_info).

Usage:
    uv run scripts/migrate_principle_annotations.py
    uv run scripts/migrate_principle_annotations.py --db ~/lobster-workspace/data/memory.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_DB = Path.home() / "lobster-workspace" / "data" / "memory.db"


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def add_principle_annotation_column(conn: sqlite3.Connection) -> str:
    """
    Add principle_annotation column to events table if not already present.

    Returns a status string describing what was done.
    """
    if not table_exists(conn, "events"):
        return "SKIPPED: events table does not exist in this database"

    if column_exists(conn, "events", "principle_annotation"):
        return "SKIPPED: principle_annotation column already exists"

    conn.execute(
        "ALTER TABLE events ADD COLUMN principle_annotation TEXT"
    )
    conn.commit()
    return "OK: added principle_annotation TEXT column to events table"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add principle_annotation column to memory.db events table"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to memory.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check whether migration is needed without applying it",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.db.exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    if args.dry_run:
        conn = sqlite3.connect(str(args.db))
        exists = column_exists(conn, "events", "principle_annotation")
        conn.close()
        status = "already exists" if exists else "migration needed"
        print(f"DRY RUN: principle_annotation column — {status}")
        return 0

    conn = sqlite3.connect(str(args.db))
    try:
        result = add_principle_annotation_column(conn)
        print(result)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
