"""
Tests for the numbered DB migration layer (issue #334).

Covers:
- Fresh DB runs all migrations in order and records them in _migrations
- Already-migrated DB is idempotent (run_migrations twice == same result)
- _migrations table is created automatically
- Migration versions are recorded with filename and applied_at
- Missing migration file raises clearly (FileNotFoundError or similar)
- 0001_initial_schema: all expected tables and view are created
- 0002_add_notes_column: notes column present with correct default
- Registry.__init__ calls run_migrations (integration smoke test)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.orchestration.migrate import run_migrations, _discover_migrations, _MIGRATIONS_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(db_path: Path) -> set[str]:
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def _column_names(db_path: Path, table: str) -> set[str]:
    conn = _open(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def _applied_versions(db_path: Path) -> list[int]:
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT version FROM _migrations ORDER BY version"
        ).fetchall()
        return [r["version"] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fresh DB: all migrations applied
# ---------------------------------------------------------------------------

class TestFreshDB:
    def test_all_migrations_applied_on_fresh_db(self, tmp_path: Path) -> None:
        """run_migrations on a new file applies every migration."""
        db_path = tmp_path / "fresh.db"
        applied = run_migrations(db_path)
        expected_versions = [v for v, _ in _discover_migrations(_MIGRATIONS_DIR)]
        assert applied == expected_versions

    def test_migrations_table_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        assert "_migrations" in _table_names(db_path)

    def test_migrations_records_versions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        expected = sorted(v for v, _ in _discover_migrations(_MIGRATIONS_DIR))
        assert _applied_versions(db_path) == expected

    def test_migrations_record_filenames(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        conn = _open(db_path)
        try:
            rows = conn.execute(
                "SELECT version, filename FROM _migrations ORDER BY version"
            ).fetchall()
            for row in rows:
                assert row["filename"].endswith(".sql")
                assert row["filename"].startswith(f"{row['version']:04d}_")
        finally:
            conn.close()

    def test_migrations_record_applied_at(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        conn = _open(db_path)
        try:
            rows = conn.execute("SELECT applied_at FROM _migrations").fetchall()
            for row in rows:
                # applied_at must be a non-empty ISO timestamp string
                assert row["applied_at"]
                assert "T" in row["applied_at"]  # ISO format
        finally:
            conn.close()

    def test_initial_schema_tables_created(self, tmp_path: Path) -> None:
        """Migration 0001 must create uow_registry and audit_log."""
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        tables = _table_names(db_path)
        assert "uow_registry" in tables
        assert "audit_log" in tables

    def test_executor_uow_view_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        conn = _open(db_path)
        try:
            views = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='executor_uow_view'"
            ).fetchone()
            assert views is not None
        finally:
            conn.close()

    def test_notes_column_present_after_migration(self, tmp_path: Path) -> None:
        """Migration 0002 must add the notes column to uow_registry."""
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        cols = _column_names(db_path, "uow_registry")
        assert "notes" in cols

    def test_notes_column_default_empty_json_object(self, tmp_path: Path) -> None:
        """notes column defaults to '{}' (empty JSON object)."""
        db_path = tmp_path / "fresh.db"
        run_migrations(db_path)
        conn = _open(db_path)
        try:
            import uuid
            from datetime import datetime, timezone
            uow_id = f"uow_{uuid.uuid4().hex[:6]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, sweep_date, status, posture, created_at, updated_at, summary)
                VALUES (?, 'executable', 'test', '2026-01-01', 'proposed', 'solo', ?, ?, 'Test')
                """,
                (uow_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT notes FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            assert row["notes"] == "{}"
        finally:
            conn.close()

    def test_creates_parent_dirs_if_needed(self, tmp_path: Path) -> None:
        """run_migrations creates intermediate directories."""
        db_path = tmp_path / "deep" / "nested" / "registry.db"
        run_migrations(db_path)
        assert db_path.exists()


# ---------------------------------------------------------------------------
# Idempotency: second run is a no-op
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_applies_nothing(self, tmp_path: Path) -> None:
        """Running run_migrations twice returns empty list on the second call."""
        db_path = tmp_path / "idem.db"
        run_migrations(db_path)
        applied_second = run_migrations(db_path)
        assert applied_second == []

    def test_second_run_does_not_duplicate_records(self, tmp_path: Path) -> None:
        """_migrations table has exactly one row per version after two runs."""
        db_path = tmp_path / "idem.db"
        run_migrations(db_path)
        run_migrations(db_path)
        conn = _open(db_path)
        try:
            rows = conn.execute("SELECT version, COUNT(*) as cnt FROM _migrations GROUP BY version").fetchall()
            for row in rows:
                assert row["cnt"] == 1, f"Version {row['version']} recorded {row['cnt']} times"
        finally:
            conn.close()

    def test_schema_unchanged_after_second_run(self, tmp_path: Path) -> None:
        """Column set is identical after first and second run."""
        db_path = tmp_path / "idem.db"
        run_migrations(db_path)
        cols_first = _column_names(db_path, "uow_registry")
        run_migrations(db_path)
        cols_second = _column_names(db_path, "uow_registry")
        assert cols_first == cols_second


# ---------------------------------------------------------------------------
# Missing file raises clearly
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_missing_migration_file_raises(self, tmp_path: Path) -> None:
        """
        If a migration file is discovered but cannot be read (e.g. deleted
        between discovery and execution), a clear exception propagates.

        We simulate this by patching _discover_migrations to return a path
        that does not exist.
        """
        db_path = tmp_path / "missing.db"
        ghost_path = tmp_path / "migrations" / "0099_ghost.sql"
        # ghost_path is intentionally NOT created

        fake_migrations = [(99, ghost_path)]
        with patch(
            "src.orchestration.migrate._discover_migrations",
            return_value=fake_migrations,
        ):
            with pytest.raises((FileNotFoundError, OSError)):
                run_migrations(db_path)

    def test_error_message_contains_path(self, tmp_path: Path) -> None:
        """The exception message or type should indicate which file failed."""
        db_path = tmp_path / "missing2.db"
        ghost_path = tmp_path / "migrations" / "0088_gone.sql"

        fake_migrations = [(88, ghost_path)]
        with patch(
            "src.orchestration.migrate._discover_migrations",
            return_value=fake_migrations,
        ):
            with pytest.raises((FileNotFoundError, OSError)) as exc_info:
                run_migrations(db_path)
            assert "0088_gone.sql" in str(exc_info.value) or "0088" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Registry integration: __init__ calls run_migrations
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    def test_registry_init_creates_tables(self, tmp_path: Path) -> None:
        """Registry.__init__ must result in all migration tables existing."""
        from src.orchestration.registry import Registry
        db_path = tmp_path / "reg.db"
        Registry(db_path)
        tables = _table_names(db_path)
        assert "uow_registry" in tables
        assert "audit_log" in tables
        assert "_migrations" in tables

    def test_registry_init_is_idempotent(self, tmp_path: Path) -> None:
        """Constructing Registry twice does not fail."""
        from src.orchestration.registry import Registry
        db_path = tmp_path / "reg.db"
        Registry(db_path)
        Registry(db_path)  # must not raise

    def test_registry_can_upsert_after_migration(self, tmp_path: Path) -> None:
        """After migration, Registry.upsert works normally."""
        from src.orchestration.registry import Registry, UpsertInserted
        db_path = tmp_path / "reg.db"
        reg = Registry(db_path)
        result = reg.upsert(issue_number=42, title="Post-migration UoW", sweep_date="2026-01-01", success_criteria="Test completion.")
        assert isinstance(result, UpsertInserted)

    def test_notes_column_accessible_after_registry_init(self, tmp_path: Path) -> None:
        """The notes column exists and is accessible after Registry.__init__."""
        from src.orchestration.registry import Registry
        db_path = tmp_path / "reg.db"
        Registry(db_path)
        cols = _column_names(db_path, "uow_registry")
        assert "notes" in cols


# ---------------------------------------------------------------------------
# _discover_migrations utility
# ---------------------------------------------------------------------------

class TestDiscoverMigrations:
    def test_discovers_numbered_files_in_order(self, tmp_path: Path) -> None:
        """_discover_migrations returns files sorted by version number."""
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "0003_third.sql").write_text("SELECT 1;")
        (mdir / "0001_first.sql").write_text("SELECT 1;")
        (mdir / "0002_second.sql").write_text("SELECT 1;")
        (mdir / "not_a_migration.txt").write_text("ignore me")

        from src.orchestration.migrate import _discover_migrations
        result = _discover_migrations(mdir)
        versions = [v for v, _ in result]
        assert versions == [1, 2, 3]

    def test_ignores_non_matching_files(self, tmp_path: Path) -> None:
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        (mdir / "README.md").write_text("docs")
        (mdir / "__init__.py").write_text("")
        (mdir / "0001_valid.sql").write_text("SELECT 1;")

        from src.orchestration.migrate import _discover_migrations
        result = _discover_migrations(mdir)
        assert len(result) == 1
        assert result[0][0] == 1
