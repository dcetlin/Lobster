"""
WOS Phase 3 — Hook System.

Structural hooks that fire on WOS lifecycle events. These hooks live in the
system repo and are not user-configurable.

Implemented hooks:
  retry-on-failure : fires on ``on_failure`` event; increments retry_count;
                     re-queues the UoW if retry_count < 3.
  loop-guard       : fires on any hook application; checks hooks_applied for a
                     hook_id appearing >= 3 times; if so, sets hooks_frozen=True
                     on the registry record and skips further hook evaluation.

# TODO: migrate behavioral hooks from user-config
# Behavioral hooks (escalation thresholds, notification preferences) are not
# implemented in this pass. A future pass will load user-config hook definitions
# from ~/lobster-user-config/orchestration/hooks.yaml (or equivalent) and merge
# them with the structural hooks defined here. The interface contract below must
# remain stable across that migration.

Interface:
    def apply_hooks(uow_id: str, event: str, registry) -> list[str]:
        '''Returns list of hook_ids that fired.'''

Events:
    on_classify          — called when a UoW is first classified
    on_state_transition  — called when a UoW transitions state
    on_failure           — called when a UoW transitions to 'failed' state

Hook IDs fired are appended to ``hooks_applied`` in the registry record (never
replaced — append semantics). If ``hooks_frozen`` is True on a UoW, hook
application is skipped entirely and a warning is logged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("hooks")

# ---------------------------------------------------------------------------
# Hook IDs (string constants — prevents typos at call sites)
# ---------------------------------------------------------------------------

HOOK_RETRY_ON_FAILURE = "retry-on-failure"
HOOK_LOOP_GUARD = "loop-guard"

# Maximum retry_count before retry-on-failure stops re-queueing.
_MAX_RETRIES = 3

# Maximum times the same hook_id may appear in hooks_applied before loop-guard
# freezes hook evaluation.
_LOOP_GUARD_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Registry helpers — raw SQLite reads/writes
# ---------------------------------------------------------------------------
# We accept a ``registry`` parameter (Registry instance) and use its db_path
# to open a fresh connection, matching the Registry's own connection pattern.
# This avoids importing Registry at module level (circular import risk) while
# keeping the hook system testable with a lightweight stub.


def _get_uow_hook_fields(db_path: Path, uow_id: str) -> dict | None:
    """
    Read hooks_applied, hooks_frozen, and retry_count for a UoW.

    Returns None if the UoW is not found. Returns a dict with:
      hooks_applied : list[str] (deserialized from JSON, default [])
      hooks_frozen  : bool (from DB column, default False)
      retry_count   : int (from DB column, default 0)
      status        : str
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        row = conn.execute(
            "SELECT hooks_applied, hooks_frozen, retry_count, status FROM uow_registry WHERE id = ?",
            (uow_id,),
        ).fetchone()
        if row is None:
            return None
        raw_hooks = row["hooks_applied"]
        try:
            hooks_applied: list[str] = json.loads(raw_hooks) if raw_hooks else []
        except (json.JSONDecodeError, TypeError):
            hooks_applied = []
        return {
            "hooks_applied": hooks_applied,
            "hooks_frozen": bool(row["hooks_frozen"]) if row["hooks_frozen"] is not None else False,
            "retry_count": row["retry_count"] if row["retry_count"] is not None else 0,
            "status": row["status"],
        }
    finally:
        conn.close()


def _append_hook_applied(db_path: Path, uow_id: str, hook_id: str) -> None:
    """Append hook_id to hooks_applied list (append, never replace)."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT hooks_applied FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return
        raw = row["hooks_applied"]
        try:
            existing: list = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            existing = []
        existing.append(hook_id)
        conn.execute(
            "UPDATE uow_registry SET hooks_applied = ? WHERE id = ?",
            (json.dumps(existing), uow_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _freeze_hooks(db_path: Path, uow_id: str) -> None:
    """Set hooks_frozen = 1 on the UoW."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE uow_registry SET hooks_frozen = 1 WHERE id = ?", (uow_id,)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _increment_retry_count(db_path: Path, uow_id: str) -> int:
    """Increment retry_count by 1 and return the new value."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT retry_count FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return 0
        current = row["retry_count"] if row["retry_count"] is not None else 0
        new_count = current + 1
        conn.execute(
            "UPDATE uow_registry SET retry_count = ? WHERE id = ?",
            (new_count, uow_id),
        )
        conn.commit()
        return new_count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _requeue_uow(db_path: Path, uow_id: str) -> None:
    """Re-queue a failed UoW to 'proposed' so it re-enters the pipeline."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE uow_registry SET status = 'proposed' WHERE id = ? AND status = 'failed'",
            (uow_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


def _hook_retry_on_failure(uow_id: str, event: str, db_path: Path) -> bool:
    """
    Structural hook: retry-on-failure.

    Fires on ``on_failure`` event only. Increments retry_count and re-queues
    the UoW to 'proposed' if retry_count < _MAX_RETRIES. Returns True if the
    hook fired (i.e., event matches and the hook ran its logic).
    """
    if event != "on_failure":
        return False

    new_count = _increment_retry_count(db_path, uow_id)
    if new_count < _MAX_RETRIES:
        _requeue_uow(db_path, uow_id)
        log.info(
            "retry-on-failure: UoW %s re-queued (retry_count=%d/%d)",
            uow_id, new_count, _MAX_RETRIES,
        )
    else:
        log.warning(
            "retry-on-failure: UoW %s has exceeded max retries (%d/%d) — not re-queueing",
            uow_id, new_count, _MAX_RETRIES,
        )
    return True


def _hook_loop_guard(uow_id: str, hook_id: str, db_path: Path, fields: dict) -> bool:
    """
    Structural hook: loop-guard.

    Fires on any hook application. Checks if hook_id already appears >= 3 times
    in hooks_applied. If so, sets hooks_frozen=True and logs a warning.

    Returns True if the loop-guard itself fired (i.e., the threshold was met
    and hooks were frozen). Returns False if the count is below threshold.
    """
    hooks_applied: list[str] = fields.get("hooks_applied") or []
    # Count occurrences of this hook_id in the current list (before appending).
    count = hooks_applied.count(hook_id)
    if count >= _LOOP_GUARD_THRESHOLD:
        old_route_reason = "unknown"
        _freeze_hooks(db_path, uow_id)
        log.warning(
            "LOOP GUARD: hooks frozen for %s — hook_id '%s' appeared %d times "
            "(threshold=%d)",
            uow_id, hook_id, count, _LOOP_GUARD_THRESHOLD,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_hooks(uow_id: str, event: str, registry) -> list[str]:
    """
    Apply all structural hooks for ``event`` on ``uow_id``.

    Returns the list of hook_ids that fired. Hook IDs are appended to
    ``hooks_applied`` in the registry record (never replaced).

    If ``hooks_frozen`` is True on the UoW, hook application is skipped
    entirely and an empty list is returned (with a log warning).

    Args:
        uow_id:   The UoW identifier.
        event:    One of 'on_classify', 'on_state_transition', 'on_failure'.
        registry: A Registry instance (provides .db_path: Path).

    Returns:
        list[str] — hook_ids that fired (may be empty).
    """
    db_path: Path = registry.db_path

    fields = _get_uow_hook_fields(db_path, uow_id)
    if fields is None:
        log.warning("apply_hooks: UoW %s not found in registry — skipping hooks", uow_id)
        return []

    if fields.get("hooks_frozen"):
        log.warning("hooks frozen for %s — skipping hook application", uow_id)
        return []

    fired: list[str] = []

    # --- retry-on-failure ---
    if _hook_retry_on_failure(uow_id, event, db_path):
        hook_id = HOOK_RETRY_ON_FAILURE
        # Re-read fields to reflect updated hooks_applied before loop-guard check.
        fields = _get_uow_hook_fields(db_path, uow_id) or fields
        # Loop-guard check for retry-on-failure itself.
        loop_fired = _hook_loop_guard(uow_id, hook_id, db_path, fields)
        if not loop_fired:
            _append_hook_applied(db_path, uow_id, hook_id)
            fired.append(hook_id)
        # If loop-guard fired, hooks are now frozen — do not append or continue.
        if fields.get("hooks_frozen") or loop_fired:
            return fired

    # --- loop-guard is a meta-hook; it fires during any hook application above ---
    # No explicit loop-guard entry in fired — it acts as a guard, not a primary hook.

    return fired
