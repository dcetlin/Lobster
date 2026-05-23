"""
WOS Phase 3 — Registry query helpers for dispatcher self-orientation.

Exposes:
  get_active_summary() — registry query for dispatcher self-orientation
                         (reads local DB only, no GitHub API calls)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("classify_intake")


# ---------------------------------------------------------------------------
# Registry query for dispatcher self-orientation
# ---------------------------------------------------------------------------

def get_active_summary(db_path: Path) -> list[dict]:
    """
    Return all non-terminal UoWs with: id, posture, route_reason, status, hooks_applied.

    Reads from the local registry DB only. No gh CLI calls, no GitHub API.
    If the registry is empty or has no active UoWs, returns [].

    Terminal statuses excluded: done, failed, expired, cancelled.

    Args:
        db_path: Path to the registry SQLite database.

    Returns:
        list[dict] — each dict has keys: id, posture, route_reason, status, hooks_applied.
    """
    from src.orchestration.registry import UoWStatus

    terminal_statuses = tuple(
        s.value for s in UoWStatus if s.is_terminal()
    )
    placeholders = ",".join("?" for _ in terminal_statuses)

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        rows = conn.execute(
            f"""
            SELECT id, posture, route_reason, status, hooks_applied
              FROM uow_registry
             WHERE status NOT IN ({placeholders})
             ORDER BY created_at DESC
            """,
            terminal_statuses,
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        raw_hooks = row["hooks_applied"]
        try:
            hooks_applied: list = json.loads(raw_hooks) if raw_hooks else []
        except (json.JSONDecodeError, TypeError):
            hooks_applied = []
        result.append({
            "id": row["id"],
            "posture": row["posture"],
            "route_reason": row["route_reason"],
            "status": row["status"],
            "hooks_applied": hooks_applied,
        })

    return result
