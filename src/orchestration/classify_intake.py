"""
WOS Phase 3 — Classifier intake wiring and registry query helpers.

This module provides the ``classify_and_register`` function that wires the
classifier into the UoW intake path without modifying registry.py. Call it
immediately after Registry.upsert() returns UpsertInserted.

Also exposes:
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
# Thrash detection helpers
# ---------------------------------------------------------------------------

def _detect_and_flag_thrash(db_path: Path, uow_id: str, new_route_reason: str) -> bool:
    """
    Check if route_reason has changed more than once for this UoW.

    Implementation:
    - Read current route_reason and classifier_thrash from the DB.
    - If route_reason already exists AND differs from new_route_reason:
        - If classifier_thrash is already True: already flagged, return True.
        - If classifier_thrash is False (first change): set it to True,
          log a warning, return True.
    - If route_reason is absent or matches: no thrash, return False.

    Writes classifier_thrash=1 atomically in a BEGIN IMMEDIATE transaction
    when flagging.

    Returns True if thrash was detected (already flagged or newly flagged).
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        row = conn.execute(
            "SELECT route_reason, classifier_thrash FROM uow_registry WHERE id = ?",
            (uow_id,),
        ).fetchone()
        if row is None:
            return False

        old_reason = row["route_reason"]
        already_thrashing = bool(row["classifier_thrash"])

        # No previous route_reason set, or same value → no thrash
        if not old_reason or old_reason == new_route_reason:
            return False

        # route_reason differs from what's in DB
        if already_thrashing:
            # Already flagged — log and return without re-writing
            log.warning(
                "CLASSIFIER THRASH: %s route_reason changed to '%s' (was '%s') — already flagged",
                uow_id, new_route_reason, old_reason,
            )
            return True

        # First change detected — flag it
        log.warning(
            "CLASSIFIER THRASH: %s route_reason changed to '%s' (was '%s')",
            uow_id, new_route_reason, old_reason,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE uow_registry SET classifier_thrash = 1 WHERE id = ?", (uow_id,)
        )
        conn.commit()
        return True

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write classifier results to registry
# ---------------------------------------------------------------------------

def _write_classify_fields(
    db_path: Path,
    uow_id: str,
    posture: str,
    route_reason: str,
    rule_name: str,
) -> None:
    """
    Write posture, route_reason, and rule_name to the registry record.

    Uses a BEGIN IMMEDIATE transaction. Does not touch any other fields.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE uow_registry
               SET posture = ?, route_reason = ?, rule_name = ?
             WHERE id = ?
            """,
            (posture, route_reason, rule_name, uow_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API — intake wiring
# ---------------------------------------------------------------------------

def classify_and_register(uow_id: str, uow: dict, registry) -> None:
    """
    Classify a UoW and write classifier results + hook firings to the registry.

    Call this immediately after Registry.upsert() returns UpsertInserted.

    Steps performed:
    1. Call classify(uow) to determine posture, rule_name, and route_reason.
    2. Run thrash detection (log + flag if route_reason changed from a prior value).
    3. Write posture, route_reason, and rule_name to the registry record.
    4. Call apply_hooks(uow_id, "on_classify", registry) — append fired hook IDs
       to hooks_applied.

    If hooks_frozen is True on the UoW, apply_hooks is skipped (logged in hooks.py).

    Args:
        uow_id:   The newly-inserted UoW id.
        uow:      A dict representation of the UoW (used for classification).
                  Typically built from the fields passed to Registry.upsert().
        registry: A Registry instance (provides .db_path).

    Raises:
        FileNotFoundError: If classifier.yaml is absent.
        ValueError: If a condition uses an unknown op.
    """
    from src.orchestration.classifier import classify
    from src.orchestration.hooks import apply_hooks

    db_path: Path = registry.db_path

    # Step 1: classify
    result = classify(uow)

    # Step 2: thrash detection (non-blocking — flag and continue)
    _detect_and_flag_thrash(db_path, uow_id, result.route_reason)

    # Step 3: write classifier fields
    _write_classify_fields(db_path, uow_id, result.posture, result.route_reason, result.rule_name)
    log.debug(
        "classify_and_register: UoW %s → posture=%s, rule=%s, route_reason=%r",
        uow_id, result.posture, result.rule_name, result.route_reason,
    )

    # Step 4: apply on_classify hooks
    fired = apply_hooks(uow_id, "on_classify", registry)
    if fired:
        log.debug("classify_and_register: hooks fired for %s: %s", uow_id, fired)


# ---------------------------------------------------------------------------
# Step 5 — Registry query for dispatcher self-orientation
# ---------------------------------------------------------------------------

def get_active_summary(db_path: Path) -> list[dict]:
    """
    Return all non-terminal UoWs with: id, posture, route_reason, status, hooks_applied.

    Reads from the local registry DB only. No gh CLI calls, no GitHub API.
    If the registry is empty or has no active UoWs, returns [].

    Terminal statuses excluded: done, failed, expired.

    Args:
        db_path: Path to the registry SQLite database.

    Returns:
        list[dict] — each dict has keys: id, posture, route_reason, status, hooks_applied.
    """
    NON_TERMINAL_STATUSES = ("done", "failed", "expired")
    placeholders = ",".join(f"'{s}'" for s in NON_TERMINAL_STATUSES)

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
            """
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
