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
    """Return {uow_id: steward_activity_count} for all UoWs with audit records.

    steward_activity_count is the number of audit_log entries written by the
    steward for each UoW. The steward writes these event strings on each cycle:
    'steward_prescription', 'steward_diagnosis', 'steward_surface',
    'steward_closure', 'agenda_update', 'reentry_prescription', 'prescription'.
    UoWs with no such entries are omitted.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT uow_id, COUNT(*) AS cycle_count
            FROM audit_log
            WHERE event IN (
                'steward_prescription',
                'steward_diagnosis',
                'steward_surface',
                'steward_closure',
                'agenda_update',
                'reentry_prescription',
                'prescription'
            )
            GROUP BY uow_id
            ORDER BY uow_id ASC
            """,
        ).fetchall()
        return {row["uow_id"]: row["cycle_count"] for row in rows}
    finally:
        conn.close()


def completed_uow_ids_since(
    since: str,
    registry_path: Path | None = None,
) -> list[str]:
    """Return UoW IDs that have an execution_complete audit entry since since_iso.

    ``since`` is an ISO-8601 string (UTC).  Returns an empty list if the
    audit_log table does not exist yet.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    try:
        conn = _connect(path)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT uow_id
            FROM audit_log
            WHERE event = 'execution_complete'
              AND ts >= ?
            """,
            (since,),
        ).fetchall()
        return [row["uow_id"] for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def execution_outcomes(
    since: datetime,
    registry_path: Path | None = None,
) -> dict[str, int]:
    """Return {outcome: count} for all executor outcomes since the given datetime.

    Executor outcomes are audit entries whose event is 'execution_complete' or
    'execution_failed' (written by Registry.complete_uow and .fail_uow).
    The outcome key is the event value itself — 'execution_complete' or
    'execution_failed'. The note column contains a JSON dict with actor,
    output_ref or reason, and timestamp; the event string is the authoritative
    outcome signal.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    since_iso = since.isoformat() if since.tzinfo is not None else since.replace(tzinfo=timezone.utc).isoformat()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT event AS outcome, COUNT(*) AS cnt
            FROM audit_log
            WHERE event IN ('execution_complete', 'execution_failed')
              AND ts >= ?
            GROUP BY event
            """,
            (since_iso,),
        ).fetchall()
        return {row["outcome"]: row["cnt"] for row in rows}
    finally:
        conn.close()


def diagnosis_events(
    registry_path: Path | None = None,
) -> list[dict]:
    """Return all steward_diagnosis and steward_prescription audit_log entries.

    Results are ordered by ts ASC so callers can reason about the sequence of
    diagnostic events without needing to re-sort.

    Each dict contains the standard audit_log columns:
        id, ts, uow_id, event, from_status, to_status, agent, note
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, ts, uow_id, event, from_status, to_status, agent, note
            FROM audit_log
            WHERE event IN ('steward_diagnosis', 'steward_prescription')
            ORDER BY ts ASC
            """,
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def terminal_outcomes(
    registry_path: Path | None = None,
) -> dict[str, str]:
    """Return {uow_id: outcome} for the latest terminal event per UoW.

    Outcome values are the event strings 'execution_complete' or
    'execution_failed'. Only the latest such event per UoW is included —
    if a UoW was failed then retried and completed, the result is
    'execution_complete'.

    UoWs with no terminal event are omitted.
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT uow_id, event
            FROM audit_log
            WHERE id IN (
                SELECT MAX(id)
                FROM audit_log
                WHERE event IN ('execution_complete', 'execution_failed')
                GROUP BY uow_id
            )
            """,
        ).fetchall()
        return {row["uow_id"]: row["event"] for row in rows}
    finally:
        conn.close()
