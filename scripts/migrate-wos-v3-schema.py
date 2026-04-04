#!/usr/bin/env python3
"""
WOS V3 Schema Migration Script

Applies migration 0007 (register field, uow_mode field, corrective_traces table,
closed_at, close_reason) to the WOS registry database.

This script is a thin wrapper around the existing migration runner. It validates
that migration 0007 applied cleanly and reports what changed.

Usage:
    uv run scripts/migrate-wos-v3-schema.py [DB_PATH]

    DB_PATH defaults to ~/lobster-workspace/orchestration/registry.db
    Override with REGISTRY_DB_PATH environment variable.

Exit codes:
    0 — migration applied or already up-to-date
    1 — migration failed
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so src.orchestration imports work.
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _default_db_path() -> Path:
    workspace = Path(os.environ.get(
        "LOBSTER_WORKSPACE",
        Path.home() / "lobster-workspace",
    ))
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return workspace / "orchestration" / "registry.db"


def _verify_schema(db_path: Path) -> dict[str, list[str]]:
    """Return a dict of {table_name: [column_names]} for V3-added objects."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    results: dict[str, list[str]] = {}
    try:
        # Check uow_registry columns
        cols = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
        results["uow_registry"] = [c["name"] for c in cols]

        # Check corrective_traces table
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "corrective_traces" in tables:
            ct_cols = conn.execute("PRAGMA table_info(corrective_traces)").fetchall()
            results["corrective_traces"] = [c["name"] for c in ct_cols]
        else:
            results["corrective_traces"] = []

        # Check executor_uow_view columns
        views = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }
        if "executor_uow_view" in views:
            view_cols = conn.execute("PRAGMA table_info(executor_uow_view)").fetchall()
            results["executor_uow_view"] = [c["name"] for c in view_cols]
        else:
            results["executor_uow_view"] = []

    finally:
        conn.close()
    return results


def main() -> int:
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        db_path = _default_db_path()

    print(f"WOS V3 Schema Migration")
    print(f"DB path: {db_path}")
    print()

    try:
        from src.orchestration.migrate import run_migrations
    except ImportError as exc:
        print(f"ERROR: Could not import migration runner: {exc}", file=sys.stderr)
        print(
            "Ensure you are running from the lobster repo root:\n"
            "  uv run scripts/migrate-wos-v3-schema.py",
            file=sys.stderr,
        )
        return 1

    try:
        applied = run_migrations(db_path)
    except Exception as exc:
        print(f"ERROR: Migration failed: {exc}", file=sys.stderr)
        return 1

    if applied:
        print(f"Applied migrations: {applied}")
    else:
        print("No migrations needed (already up-to-date).")

    # Verify V3 fields are present
    schema = _verify_schema(db_path)

    uow_cols = schema.get("uow_registry", [])
    missing_uow = [c for c in ("register", "uow_mode", "closed_at", "close_reason") if c not in uow_cols]
    if missing_uow:
        print(f"ERROR: Missing V3 columns in uow_registry: {missing_uow}", file=sys.stderr)
        return 1

    ct_cols = schema.get("corrective_traces", [])
    if not ct_cols:
        print("ERROR: corrective_traces table not found", file=sys.stderr)
        return 1
    missing_ct = [c for c in ("uow_id", "register", "execution_summary", "gate_score") if c not in ct_cols]
    if missing_ct:
        print(f"ERROR: Missing columns in corrective_traces: {missing_ct}", file=sys.stderr)
        return 1

    view_cols = schema.get("executor_uow_view", [])
    missing_view = [c for c in ("register", "uow_mode") if c not in view_cols]
    if missing_view:
        print(f"ERROR: Missing V3 columns in executor_uow_view: {missing_view}", file=sys.stderr)
        return 1

    print()
    print("Schema verification PASSED:")
    print(f"  uow_registry V3 columns present: register, uow_mode, closed_at, close_reason")
    print(f"  corrective_traces table present: {ct_cols}")
    print(f"  executor_uow_view V3 columns present: register, uow_mode")
    print()
    print("WOS V3 schema migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
