#!/usr/bin/env python3
"""
PostToolUse hook: routes decision: footer blocks to the decisions ledger.

When a send_reply message contains a ```decision: ... ``` code block,
extract the content and write it to the decisions table in memory.db.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone


DECISION_BLOCK_RE = re.compile(r"```decision:\s*\n(.*?)```", re.DOTALL)
DB_PATH = os.path.expanduser("~/lobster-workspace/data/memory.db")


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


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "mcp__lobster-inbox__send_reply":
        sys.exit(0)

    text = data.get("tool_input", {}).get("text", "")
    if not text:
        sys.exit(0)

    match = DECISION_BLOCK_RE.search(text)
    if not match:
        sys.exit(0)

    decision_text = match.group(1).strip()
    if not decision_text:
        sys.exit(0)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH) as conn:
        ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO decisions (date, summary, source) VALUES (?, ?, ?)",
            (date_str, decision_text, "hook"),
        )
        conn.commit()

    sys.exit(0)


if __name__ == "__main__":
    main()
