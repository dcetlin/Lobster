#!/usr/bin/env python3
"""
migrate_dedup_proposed_uows.py — One-time cleanup of duplicate proposed UoWs.

For each GitHub issue that has more than one UoW in 'proposed' status,
keep the most recently created record and expire all older duplicates.

This corrects a population of duplicates created before the upsert
idempotency check was hardened (issue #833).

Usage:
    uv run scripts/migrate_dedup_proposed_uows.py [--dry-run] [--db-path PATH]

Exit codes:
    0  — success (or dry-run)
    1  — error
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

_TERMINAL_STATUSES = ("done", "failed", "expired", "cancelled")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_default_db_path() -> Path:
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


def _find_duplicate_proposed_groups(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Return {issue_number: [row, ...]} for issues with > 1 proposed UoW.

    Rows are ordered oldest-first so the last element is the keeper.
    """
    rows = conn.execute(
        "SELECT id, source_issue_number, created_at FROM uow_registry "
        "WHERE status = 'proposed' ORDER BY source_issue_number, created_at"
    ).fetchall()

    by_issue: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        issue_num = row["source_issue_number"]
        if issue_num is None:
            continue
        by_issue[issue_num].append({"id": row["id"], "created_at": row["created_at"]})

    return {k: v for k, v in by_issue.items() if len(v) > 1}


def run(db_path: Path, dry_run: bool) -> int:
    """Expire duplicate proposed UoWs, keeping the newest per source issue.

    Returns the count of UoWs expired.
    """
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        return -1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    duplicate_groups = _find_duplicate_proposed_groups(conn)

    if not duplicate_groups:
        print("No duplicate proposed UoWs found — nothing to do.")
        conn.close()
        return 0

    total_issues = len(duplicate_groups)
    total_to_expire = sum(len(rows) - 1 for rows in duplicate_groups.values())
    print(f"Found {total_issues} issue(s) with duplicate proposed UoWs ({total_to_expire} to expire).")

    if dry_run:
        for issue_num, rows in sorted(duplicate_groups.items()):
            keeper = rows[-1]
            to_expire = rows[:-1]
            print(f"  issue #{issue_num}: keep {keeper['id']}, expire {[r['id'] for r in to_expire]}")
        print("[dry-run] No changes written.")
        conn.close()
        return 0

    expired_count = 0
    now = _now_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        for issue_num, rows in duplicate_groups.items():
            keeper = rows[-1]
            to_expire = rows[:-1]
            for row in to_expire:
                uow_id = row["id"]
                conn.execute(
                    "UPDATE uow_registry SET status='expired', updated_at=?, closed_at=?, close_reason=? WHERE id=? AND status='proposed'",
                    (now, now, "dedup_migration: older duplicate, kept " + keeper["id"], uow_id),
                )
                # Audit entry
                conn.execute(
                    "INSERT INTO audit_log (uow_id, event, ts, from_status, to_status, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (uow_id, "expired", now, "proposed", "expired", f"dedup_migration: duplicate proposed UoW expired; kept {keeper['id']}"),
                )
                expired_count += 1
                print(f"  Expired {uow_id} (issue #{issue_num}), kept {keeper['id']}")

        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: rollback after exception: {exc}", file=sys.stderr)
        conn.close()
        return -1

    conn.close()
    print(f"Done. {expired_count} duplicate proposed UoW(s) expired.")
    return expired_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing.")
    parser.add_argument("--db-path", type=Path, default=None, help="Path to registry.db (default: auto-detect).")
    args = parser.parse_args()

    db_path = args.db_path or _get_default_db_path()
    result = run(db_path, dry_run=args.dry_run)
    sys.exit(0 if result >= 0 else 1)


if __name__ == "__main__":
    main()
