"""
audit_queries.py — Read-only query layer over the audit_log table.

The audit_log table is written by registry.py's record_* methods on every
UoW state transition. This module exposes pure query functions that make
audit history visible: recent transitions per UoW, stall events in a time
window, a histogram of steward cycle counts, and a breakdown of executor
outcomes.

All functions open their own connection (matching the _connect() pattern in
registry.py), execute a single query, and return plain dicts. No writes, no
dataclasses.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_registry_path() -> Path:
    """Return the canonical registry DB path, matching registry_cli.py's _get_db_path()."""
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


def _connect(registry_path: Path) -> sqlite3.Connection:
    """Open a read-only connection with WAL and row_factory, matching registry.py."""
    conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------

def recent_transitions(
    uow_id: str,
    limit: int = 20,
    registry_path: Path | None = None,
) -> list[dict]:
    """Return recent audit_log entries for a UoW, newest first.

    Each dict has keys: id, ts, uow_id, event, from_status, to_status,
    agent, note — exactly the audit_log columns.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, ts, uow_id, event, from_status, to_status, agent, note
            FROM audit_log
            WHERE uow_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (uow_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def stall_events(
    since: datetime,
    registry_path: Path | None = None,
) -> list[dict]:
    """Return all stall_detected audit entries since the given datetime.

    The datetime is compared against the audit_log.ts column (ISO-8601 strings
    stored in UTC). If ``since`` has no tzinfo, it is treated as UTC.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    since_iso = since.isoformat() if since.tzinfo is not None else since.replace(tzinfo=timezone.utc).isoformat()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, ts, uow_id, event, from_status, to_status, agent, note
            FROM audit_log
            WHERE event = 'stall_detected'
              AND ts >= ?
            ORDER BY ts ASC
            """,
            (since_iso,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def cycles_histogram(
    registry_path: Path | None = None,
) -> dict[str, int]:
    """Return {uow_id: steward_cycle_count} for all UoWs with audit records.

    steward_cycle_count is the number of audit_log entries whose event is
    'steward_cycle' for each UoW. UoWs with no such entries are omitted.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT uow_id, COUNT(*) AS cycle_count
            FROM audit_log
            WHERE event = 'steward_cycle'
            GROUP BY uow_id
            ORDER BY uow_id ASC
            """,
        ).fetchall()
        return {row["uow_id"]: row["cycle_count"] for row in rows}
    finally:
        conn.close()


def execution_outcomes(
    since: datetime,
    registry_path: Path | None = None,
) -> dict[str, int]:
    """Return {outcome: count} for all executor outcomes since the given datetime.

    Executor outcomes are audit entries whose event is 'executor_outcome'.
    The outcome value is stored in the audit_log.note column.
    Entries with a NULL note are counted under the key 'unknown'.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    since_iso = since.isoformat() if since.tzinfo is not None else since.replace(tzinfo=timezone.utc).isoformat()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(note, 'unknown') AS outcome, COUNT(*) AS cnt
            FROM audit_log
            WHERE event = 'executor_outcome'
              AND ts >= ?
            GROUP BY COALESCE(note, 'unknown')
            """,
            (since_iso,),
        ).fetchall()
        return {row["outcome"]: row["cnt"] for row in rows}
    finally:
        conn.close()
