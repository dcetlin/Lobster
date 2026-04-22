"""
Unit tests for migrate_dedup_proposed_uows.py

Tests verify that the migration:
- Expires older duplicate proposed UoWs, keeping the newest per source issue
- Is a no-op when no duplicates exist
- Writes audit log entries for each expired UoW
- Leaves non-duplicate proposed UoWs untouched
- Operates correctly in dry-run mode (no writes)
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent.parent.parent / "scripts"


def _run_migration(db_path: Path, dry_run: bool = False) -> int:
    """Import and call run() directly (avoids subprocess overhead)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_dedup_proposed_uows",
        _SCRIPTS_DIR / "migrate_dedup_proposed_uows.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run(db_path, dry_run=dry_run)


def _build_registry(db_path: Path) -> sqlite3.Connection:
    """Create a minimal registry DB with schema for testing."""
    # Import Registry to run migrations
    import sys
    repo_root = Path(__file__).parent.parent.parent.parent
    if str(repo_root / "src") not in sys.path:
        sys.path.insert(0, str(repo_root / "src"))
    from orchestration.registry import Registry
    Registry(db_path)  # runs schema migrations
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _insert_proposed(conn: sqlite3.Connection, issue_num: int, uow_id: str, created_at: str) -> None:
    conn.execute(
        """INSERT INTO uow_registry
           (id, type, source, source_issue_number, sweep_date, status, posture, created_at, updated_at, summary, success_criteria)
           VALUES (?, 'executable', ?, ?, ?, 'proposed', 'solo', ?, ?, 'Test', 'Completion criteria.')""",
        (uow_id, f"github:issue/{issue_num}", issue_num,
         created_at[:10],  # sweep_date = date portion
         created_at, created_at),
    )
    conn.commit()


def _utc(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestNoDuplicates:
    def test_no_op_when_no_duplicates(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 100, "uow_100_a", _utc(1))
        conn.close()

        result = _run_migration(db)
        assert result == 0, "Expected 0 expired when no duplicates"

    def test_single_proposed_per_issue_untouched(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 200, "uow_200_a", _utc(3))
        _insert_proposed(conn, 201, "uow_201_a", _utc(2))
        conn.close()

        result = _run_migration(db)
        assert result == 0

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, status FROM uow_registry ORDER BY id").fetchall()
        assert all(r["status"] == "proposed" for r in rows)
        conn.close()


class TestDuplicateExpiry:
    def test_keeps_newest_expires_older(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        older = _utc(5)
        newer = _utc(1)
        _insert_proposed(conn, 300, "uow_300_old", older)
        _insert_proposed(conn, 300, "uow_300_new", newer)
        conn.close()

        result = _run_migration(db)
        assert result == 1, "Expected exactly 1 UoW expired"

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM uow_registry").fetchall()}
        conn.close()

        assert rows["uow_300_new"] == "proposed", "Newest must remain proposed"
        assert rows["uow_300_old"] == "expired", "Older must be expired"

    def test_three_duplicates_keeps_newest(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 400, "uow_400_old1", _utc(10))
        _insert_proposed(conn, 400, "uow_400_old2", _utc(5))
        _insert_proposed(conn, 400, "uow_400_new", _utc(1))
        conn.close()

        result = _run_migration(db)
        assert result == 2, "Expected 2 UoWs expired (keep 1 of 3)"

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM uow_registry").fetchall()}
        conn.close()

        assert rows["uow_400_new"] == "proposed"
        assert rows["uow_400_old1"] == "expired"
        assert rows["uow_400_old2"] == "expired"

    def test_audit_entries_written_for_expired_uows(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 500, "uow_500_old", _utc(3))
        _insert_proposed(conn, 500, "uow_500_new", _utc(1))
        conn.close()

        _run_migration(db)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id=? AND event='expired'", ("uow_500_old",)
        ).fetchall()
        conn.close()

        assert len(audit) == 1, "Exactly one audit entry per expired UoW"
        assert audit[0]["from_status"] == "proposed"
        assert audit[0]["to_status"] == "expired"

    def test_multiple_issues_each_deduped_independently(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 600, "uow_600_old", _utc(5))
        _insert_proposed(conn, 600, "uow_600_new", _utc(1))
        _insert_proposed(conn, 601, "uow_601_old", _utc(4))
        _insert_proposed(conn, 601, "uow_601_new", _utc(2))
        # Non-duplicate issue — must not be touched
        _insert_proposed(conn, 602, "uow_602_only", _utc(3))
        conn.close()

        result = _run_migration(db)
        assert result == 2

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM uow_registry").fetchall()}
        conn.close()

        assert rows["uow_600_new"] == "proposed"
        assert rows["uow_600_old"] == "expired"
        assert rows["uow_601_new"] == "proposed"
        assert rows["uow_601_old"] == "expired"
        assert rows["uow_602_only"] == "proposed", "Non-duplicate must not be touched"


class TestDryRun:
    def test_dry_run_makes_no_changes(self, tmp_path):
        db = tmp_path / "registry.db"
        conn = _build_registry(db)
        _insert_proposed(conn, 700, "uow_700_old", _utc(5))
        _insert_proposed(conn, 700, "uow_700_new", _utc(1))
        conn.close()

        result = _run_migration(db, dry_run=True)
        assert result == 0, "Dry-run returns 0"

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, status FROM uow_registry").fetchall()
        conn.close()
        assert all(r["status"] == "proposed" for r in rows), "Dry-run must not write"
