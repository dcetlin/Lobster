#!/usr/bin/env python3
"""
migrate_add_steward_fields.py — WOS Phase 2 PR0 schema migration.

Adds all Phase 2 fields to uow_registry and creates executor_uow_view.

This script is idempotent: it checks PRAGMA table_info(uow_registry) before
each ALTER TABLE ADD COLUMN, so it is safe to run multiple times and safe to
re-run after a partial execution (crash after column N adds only remaining
columns on re-run).

Usage:
    uv run scripts/migrate_add_steward_fields.py

Environment:
    REGISTRY_DB_PATH — override the default db path
                       (default: ~/lobster-workspace/orchestration/registry.db)

Phase 2 concurrent-writer note:
    Phase 2 introduces concurrent writers: Steward heartbeat, Executor, and
    Observation Loop. All scripts that open the registry DB must set
    PRAGMA busy_timeout = 5000 immediately after sqlite3.connect() so that the
    second writer waits up to 5 seconds rather than failing immediately.
    This script sets busy_timeout = 5000 as required.

Column visibility contract:
    Every column added here must declare its executor visibility:
    - Executor-accessible: included in executor_uow_view SELECT list.
    - Steward-private or system-only: explicitly excluded from executor_uow_view,
      with a comment here explaining why.

    This is a standing schema convention that applies to all future migrations.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Column definitions: (name, DDL fragment, executor_visible)
# ---------------------------------------------------------------------------

# Each tuple: (column_name, alter_table_type_clause, included_in_executor_view)
#
# executor_visible=True  → column appears in executor_uow_view SELECT list
# executor_visible=False → column is intentionally excluded (comment explains why)

_PHASE2_COLUMNS: list[tuple[str, str, bool]] = [
    # Absolute path to the workflow artifact JSON written by the Steward.
    # Executor reads this to find its instructions. Executor-accessible.
    ("workflow_artifact", "TEXT NULL", True),

    # Prose statement of what completion looks like. Written at germination;
    # immutable thereafter. Executor-accessible (used in evaluate_condition).
    ("success_criteria", "TEXT NULL", True),

    # Skill IDs to load at Executor task start. JSON array string (same
    # pattern as hooks_applied). NULL = not yet prescribed. Executor-accessible.
    ("prescribed_skills", "TEXT NULL", True),

    # Count of Steward diagnosis+prescription cycles completed.
    # Surface condition 3 fires when this reaches 5. Executor-accessible
    # (Executor reads it to surface condition checks).
    ("steward_cycles", "INTEGER NOT NULL DEFAULT 0", True),

    # Computed as started_at + estimated_runtime (or started_at + 1800 if
    # estimated_runtime is NULL). Observation Loop reads this on each pass
    # for stall detection. Required by #305 and #306. Executor-accessible.
    ("timeout_at", "TEXT NULL", True),

    # Optional seconds estimate. Drives timeout_at computation. Executor-
    # accessible (Executor writes timeout_at using this value at claim time).
    ("estimated_runtime", "INTEGER NULL", True),

    # Forward forecast written at first contact (steward_cycles == 0).
    # Oracle-style list/tree of anticipated prescription nodes. Updated on
    # re-entry. STEWARD-PRIVATE: excluded from executor_uow_view to enforce
    # the isolation contract at the DB layer.
    ("steward_agenda", "TEXT NULL", False),

    # Append-only log of every Steward decision point. Newline-delimited JSON.
    # STEWARD-PRIVATE: excluded from executor_uow_view to enforce isolation.
    # The Executor MUST NOT read this field.
    ("steward_log", "TEXT NULL", False),
]

# Fields visible to the Executor (included in executor_uow_view).
# Phase 1 fields already present in the table are also included here.
_EXECUTOR_VIEW_PHASE1_FIELDS = (
    "id", "status", "output_ref", "started_at", "completed_at",
    "source_issue_number", "summary",
)


def _get_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get(
        "LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")
    )
    return Path(workspace) / "orchestration" / "registry.db"


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names currently in the given table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """
    Check each Phase 2 column individually and add only those that are absent.

    Returns the list of column names that were added.
    """
    existing = _existing_columns(conn, "uow_registry")
    added: list[str] = []

    for col_name, col_type, _ in _PHASE2_COLUMNS:
        if col_name in existing:
            print(f"  [skip]  {col_name} — already present")
            continue
        conn.execute(
            f"ALTER TABLE uow_registry ADD COLUMN {col_name} {col_type}"
        )
        conn.commit()
        print(f"  [added] {col_name} {col_type}")
        added.append(col_name)

    return added


def _create_executor_uow_view(conn: sqlite3.Connection) -> None:
    """
    (Re-)create the executor_uow_view that enforces Steward-private field
    isolation at the DB layer.

    steward_agenda: Steward-private — excluded from executor_uow_view.
        Steward writes its forward forecast here; Executor must never read it.
    steward_log: Steward-private — excluded from executor_uow_view.
        Steward writes decision-point log here; Executor must never read it.

    All other Phase 2 columns are Executor-accessible and are included.
    """
    # Build the executor-visible column list from Phase 1 fields + Phase 2 fields
    # marked executor_visible=True.
    phase2_executor_cols = [
        col_name
        for col_name, _, executor_visible in _PHASE2_COLUMNS
        if executor_visible
    ]
    all_executor_cols = list(_EXECUTOR_VIEW_PHASE1_FIELDS) + phase2_executor_cols
    select_cols = ",\n    ".join(all_executor_cols)

    # DROP and re-CREATE so the view is always up to date even on re-runs.
    conn.execute("DROP VIEW IF EXISTS executor_uow_view")
    conn.execute(
        f"""
        CREATE VIEW executor_uow_view AS
        SELECT
            {select_cols}
        FROM uow_registry
        """
        # steward_agenda: Steward-private, excluded from executor_uow_view.
        #   Steward writes forward forecast here; Executor must never read it.
        # steward_log: Steward-private, excluded from executor_uow_view.
        #   Steward writes decision-point log here; Executor must never read it.
    )
    conn.commit()
    print("  [view]  executor_uow_view created/updated")


def migrate(db_path: Path) -> None:
    """Run the full Phase 2 schema migration against db_path."""
    if not db_path.parent.exists():
        print(f"ERROR: parent directory does not exist: {db_path.parent}", file=sys.stderr)
        sys.exit(1)

    print(f"Opening registry: {db_path}")
    conn = sqlite3.connect(str(db_path))
    # Phase 2 has concurrent writers (Steward heartbeat + Executor + Observation
    # Loop). Set busy_timeout so that the second writer waits rather than fails
    # immediately with SQLITE_BUSY.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # Verify the uow_registry table exists before attempting migration.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "uow_registry" not in tables:
            print(
                "ERROR: uow_registry table not found. "
                "Run the base schema initialization first (Registry.__init__).",
                file=sys.stderr,
            )
            sys.exit(1)

        print("Adding Phase 2 columns (idempotent per-column):")
        added = _add_missing_columns(conn)

        print("Creating executor_uow_view:")
        _create_executor_uow_view(conn)

        if added:
            print(f"\nMigration complete. Added {len(added)} column(s): {added}")
        else:
            print("\nMigration complete. No new columns needed (already up to date).")

    finally:
        conn.close()


if __name__ == "__main__":
    db_path = _get_db_path()
    migrate(db_path)
