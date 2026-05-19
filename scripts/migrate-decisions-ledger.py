#!/usr/bin/env python3
"""
One-off migration: parse decisions-ledger.md and insert entries into the
decisions table in memory.db. Re-runnable safely via INSERT OR IGNORE.

WOS-UoW: uow_20260502_a1a5e3
"""

import os
import re
import sqlite3
import sys

LEDGER_PATH = os.path.expanduser("~/lobster-workspace/data/decisions-ledger.md")
DB_PATH = os.path.expanduser("~/lobster-workspace/data/memory.db")

ENTRY_RE = re.compile(r"\*\*(\d{4}-\d{2}-\d{2})\*\*\s+—\s+(.+)", re.DOTALL)


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decisions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT NOT NULL,
            category   TEXT,
            task_id    TEXT,
            summary    TEXT NOT NULL,
            source     TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedup
            ON decisions(date, summary);
        CREATE INDEX IF NOT EXISTS idx_decisions_category
            ON decisions(category);
        CREATE INDEX IF NOT EXISTS idx_decisions_task_id
            ON decisions(task_id);
        CREATE INDEX IF NOT EXISTS idx_decisions_date
            ON decisions(date);
    """)
    conn.commit()


def parse_ledger(path: str) -> list[tuple[str, str]]:
    """Return list of (date, summary) tuples parsed from the flat-file ledger."""
    if not os.path.exists(path):
        return []

    with open(path) as f:
        content = f.read()

    # Split on --- separators; skip everything before the first ---
    parts = content.split("\n---\n")
    entries = []
    for part in parts[1:]:  # parts[0] is the header block
        part = part.strip()
        if not part:
            continue
        m = ENTRY_RE.match(part)
        if m:
            date_str = m.group(1)
            summary = m.group(2).strip()
            entries.append((date_str, summary))
    return entries


def main() -> None:
    entries = parse_ledger(LEDGER_PATH)

    with sqlite3.connect(DB_PATH) as conn:
        ensure_table(conn)
        migrated = 0
        for date_str, summary in entries:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO decisions (date, summary, source) VALUES (?, ?, ?)",
                (date_str, summary, "migrated"),
            )
            migrated += cursor.rowcount
        conn.commit()

    print(f"Migrated {migrated} entries from decisions-ledger.md (total parsed: {len(entries)})")


if __name__ == "__main__":
    main()
