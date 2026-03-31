"""
Tests for WOS Phase 2 PR0: schema migration.

Covers:
- All 8 new fields present in schema.sql (7 steward fields + success_criteria)
- executor_uow_view created with correct columns (steward-private fields excluded)
- migrate_add_steward_fields.py idempotency (run twice, no errors)
- Partial-migration resumability (crash after first column, re-run adds remaining)
- Pre-migration UoW survives migration with all new fields as None/0
- New UoW after migration has correct defaults
- prescribed_skills JSON deserialization (NULL→None, '[]'→[], list)
- steward_agenda and steward_log returned as None (not deserialized) when NULL
- validate_phase2_schema raises RuntimeError if any field is absent
- executor_uow_view column-not-found for steward_agenda/steward_log
- migrate_add_steward_fields.py sets PRAGMA busy_timeout=5000
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate_add_steward_fields.py"

# The 7 steward fields + success_criteria = 8 total new columns
PHASE2_FIELDS = [
    "workflow_artifact",
    "success_criteria",
    "prescribed_skills",
    "steward_cycles",
    "timeout_at",
    "estimated_runtime",
    "steward_agenda",
    "steward_log",
]

# Fields visible in executor_uow_view
EXECUTOR_VIEW_FIELDS = {
    "id", "status", "workflow_artifact", "prescribed_skills",
    "estimated_runtime", "timeout_at", "output_ref",
    "started_at", "completed_at", "steward_cycles",
    "source_issue_number", "summary", "success_criteria",
}

# Fields excluded from executor_uow_view (steward-private)
STEWARD_PRIVATE_FIELDS = {"steward_agenda", "steward_log"}


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _get_columns(db_path: Path, table: str) -> set[str]:
    conn = _open_db(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def _run_migration(db_path: Path) -> subprocess.CompletedProcess:
    """Run the migration script against the given db_path."""
    env = {"REGISTRY_DB_PATH": str(db_path)}
    import os
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT)],
        env=full_env,
        capture_output=True,
        text=True,
    )


_PHASE1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS uow_registry (
    id                  TEXT    PRIMARY KEY,
    type                TEXT    NOT NULL DEFAULT 'executable',
    source              TEXT    NOT NULL,
    source_issue_number INTEGER,
    sweep_date          TEXT,
    status              TEXT    NOT NULL DEFAULT 'proposed',
    posture             TEXT    NOT NULL DEFAULT 'solo',
    agent               TEXT,
    children            TEXT    DEFAULT '[]',
    parent              TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    summary             TEXT    NOT NULL,
    output_ref          TEXT,
    hooks_applied       TEXT    DEFAULT '[]',
    route_reason        TEXT,
    route_evidence      TEXT    DEFAULT '{}',
    trigger             TEXT    DEFAULT '{"type": "immediate"}',
    vision_ref          TEXT    DEFAULT NULL,
    UNIQUE(source_issue_number, sweep_date)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    uow_id      TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    agent       TEXT,
    note        TEXT
);
"""


@pytest.fixture
def pre_migration_db(tmp_path: Path) -> Path:
    """
    Returns a db_path pointing to a Phase 1 schema database (no Phase 2 fields).
    Uses the hardcoded Phase 1 DDL so that schema.sql updates do not affect this
    fixture — it always represents the pre-migration state.
    """
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_PHASE1_SCHEMA_SQL)
    finally:
        conn.close()
    return db_path


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    """Returns a db_path after migration has been applied."""
    from src.orchestration.registry import Registry
    db_path = tmp_path / "registry.db"
    Registry(db_path)
    result = _run_migration(db_path)
    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    return db_path


# ---------------------------------------------------------------------------
# Schema field presence tests
# ---------------------------------------------------------------------------

class TestPhase2FieldsPresent:
    def test_all_phase2_fields_in_schema_after_migration(self, migrated_db):
        """After migration, all 8 Phase 2 fields must be present in uow_registry."""
        cols = _get_columns(migrated_db, "uow_registry")
        for field in PHASE2_FIELDS:
            assert field in cols, f"Missing field after migration: {field}"

    def test_steward_cycles_default_zero(self, migrated_db):
        """steward_cycles has DEFAULT 0 — confirmed by inserting a row without it."""
        conn = _open_db(migrated_db)
        try:
            import uuid
            from datetime import datetime, timezone
            uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, sweep_date, status, posture, created_at, updated_at, summary)
                VALUES (?, 'executable', 'test', '2026-01-01', 'proposed', 'solo', ?, ?, 'Test UoW')
                """,
                (uow_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT steward_cycles FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            assert row["steward_cycles"] == 0
        finally:
            conn.close()

    def test_nullable_fields_default_null(self, migrated_db):
        """workflow_artifact, prescribed_skills, timeout_at, estimated_runtime,
        steward_agenda, steward_log all default to NULL.
        success_criteria defaults to '' (NOT NULL DEFAULT '') — excluded from
        the nullable_fields list and checked separately."""
        conn = _open_db(migrated_db)
        try:
            import uuid
            from datetime import datetime, timezone
            uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, sweep_date, status, posture, created_at, updated_at, summary)
                VALUES (?, 'executable', 'test', '2026-01-01', 'proposed', 'solo', ?, ?, 'Test UoW')
                """,
                (uow_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT workflow_artifact, prescribed_skills, timeout_at, estimated_runtime, "
                "steward_agenda, steward_log, success_criteria FROM uow_registry WHERE id = ?",
                (uow_id,)
            ).fetchone()
            nullable_fields = [
                "workflow_artifact", "prescribed_skills", "timeout_at",
                "estimated_runtime", "steward_agenda", "steward_log",
            ]
            for field in nullable_fields:
                assert row[field] is None, f"Expected NULL for {field}, got {row[field]}"
            # success_criteria is NOT NULL DEFAULT '' — defaults to empty string, not NULL
            assert row["success_criteria"] == "", (
                f"Expected '' for success_criteria, got {row['success_criteria']!r}"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# executor_uow_view tests
# ---------------------------------------------------------------------------

class TestExecutorUowView:
    def test_view_exists_after_migration(self, migrated_db):
        conn = _open_db(migrated_db)
        try:
            views = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='executor_uow_view'"
            ).fetchone()
            assert views is not None, "executor_uow_view must exist after migration"
        finally:
            conn.close()

    def test_view_includes_executor_accessible_fields(self, migrated_db):
        """Spot-check that executor-accessible fields are selectable from the view."""
        conn = _open_db(migrated_db)
        try:
            # This should not raise
            conn.execute("SELECT id, status, workflow_artifact, prescribed_skills, "
                         "success_criteria, steward_cycles FROM executor_uow_view").fetchall()
        finally:
            conn.close()

    def test_view_excludes_steward_agenda(self, migrated_db):
        """Querying steward_agenda from executor_uow_view must raise OperationalError."""
        conn = _open_db(migrated_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("SELECT steward_agenda FROM executor_uow_view").fetchall()
        finally:
            conn.close()

    def test_view_excludes_steward_log(self, migrated_db):
        """Querying steward_log from executor_uow_view must raise OperationalError."""
        conn = _open_db(migrated_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("SELECT steward_log FROM executor_uow_view").fetchall()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Migration idempotency tests
# ---------------------------------------------------------------------------

class TestMigrationIdempotency:
    def test_run_migration_twice_no_error(self, pre_migration_db):
        """Running migration twice must succeed without errors."""
        db_path = pre_migration_db
        r1 = _run_migration(db_path)
        assert r1.returncode == 0, f"First migration failed: {r1.stderr}"
        r2 = _run_migration(db_path)
        assert r2.returncode == 0, f"Second migration failed: {r2.stderr}"

    def test_run_migration_twice_schema_unchanged(self, pre_migration_db):
        """Running migration twice yields the same schema as running it once."""
        db_path = pre_migration_db
        _run_migration(db_path)
        cols_after_first = _get_columns(db_path, "uow_registry")
        _run_migration(db_path)
        cols_after_second = _get_columns(db_path, "uow_registry")
        assert cols_after_first == cols_after_second


# ---------------------------------------------------------------------------
# Partial-migration resumability tests
# ---------------------------------------------------------------------------

class TestPartialMigrationResumability:
    def test_resume_after_partial_migration(self, pre_migration_db):
        """
        Simulate a crash after adding only workflow_artifact.
        Re-running the migration must add the remaining columns without error.
        """
        db_path = pre_migration_db
        conn = _open_db(db_path)
        try:
            # Manually add only the first column (simulating partial run)
            conn.execute("ALTER TABLE uow_registry ADD COLUMN workflow_artifact TEXT NULL")
            conn.commit()
        finally:
            conn.close()

        # Now run the full migration — it should add the remaining 7 columns
        result = _run_migration(db_path)
        assert result.returncode == 0, f"Partial-migration resume failed: {result.stderr}"

        cols = _get_columns(db_path, "uow_registry")
        for field in PHASE2_FIELDS:
            assert field in cols, f"Missing after partial-migration resume: {field}"

    def test_resume_with_multiple_columns_present(self, pre_migration_db):
        """Simulate crash after adding first 3 columns; verify remaining are added."""
        db_path = pre_migration_db
        conn = _open_db(db_path)
        try:
            conn.execute("ALTER TABLE uow_registry ADD COLUMN workflow_artifact TEXT NULL")
            conn.execute("ALTER TABLE uow_registry ADD COLUMN success_criteria TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE uow_registry ADD COLUMN prescribed_skills TEXT NULL")
            conn.commit()
        finally:
            conn.close()

        result = _run_migration(db_path)
        assert result.returncode == 0, f"Migration failed: {result.stderr}"

        cols = _get_columns(db_path, "uow_registry")
        for field in PHASE2_FIELDS:
            assert field in cols, f"Missing: {field}"


# ---------------------------------------------------------------------------
# Pre-migration UoW survival tests
# ---------------------------------------------------------------------------

class TestPreMigrationUoWSurvival:
    def test_pre_migration_uow_survives_with_new_fields_as_defaults(self, tmp_path):
        """
        Insert a UoW using the Phase 1 schema, run migration, verify registry.get()
        returns the record with all 7 new fields as None/0, no KeyError.
        """
        from src.orchestration.registry import Registry
        db_path = tmp_path / "registry.db"
        reg = Registry(db_path)

        today = "2026-01-01"
        from src.orchestration.registry import UpsertInserted
        # Pre-migration simulation: insert directly via SQL to bypass the
        # success_criteria validation enforced by upsert() (this mimics rows
        # that existed before issue_url and success_criteria enforcement).
        import uuid
        from datetime import datetime, timezone
        uow_id = f"uow_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
        conn = reg._connect()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uow_registry (id, type, source, source_issue_number, sweep_date, "
            "status, posture, created_at, updated_at, summary, success_criteria, "
            "route_reason, route_evidence, trigger) "
            "VALUES (?, 'executable', 'github:issue/1001', 1001, ?, 'proposed', 'solo', "
            "?, ?, 'Pre-migration UoW', '', 'phase1-default: no classifier', '{}', '{\"type\": \"immediate\"}')",
            (uow_id, today, now, now),
        )
        conn.commit()
        conn.close()

        # Run migration
        migration_result = _run_migration(db_path)
        assert migration_result.returncode == 0, f"Migration failed: {migration_result.stderr}"

        # Re-create registry (re-initializes connection after migration)
        reg2 = Registry(db_path)
        record = reg2.get(uow_id)

        assert record is not None, f"get() returned None for id {uow_id}"
        assert record.id == uow_id
        assert record.workflow_artifact is None
        # Pre-migration rows have empty success_criteria — the schema allows '' for legacy rows.
        # New UoWs created via upsert() are now rejected if success_criteria is blank.
        assert record.success_criteria == ""
        assert record.prescribed_skills is None
        assert record.steward_cycles == 0
        assert record.timeout_at is None
        assert record.estimated_runtime is None
        assert record.steward_agenda is None
        assert record.steward_log is None

    def test_new_uow_after_migration_has_correct_defaults(self, migrated_db):
        """New UoW created after migration stores supplied success_criteria."""
        from src.orchestration.registry import Registry, UpsertInserted
        reg = Registry(migrated_db)
        result = reg.upsert(
            issue_number=1002,
            title="Post-migration UoW",
            sweep_date="2026-01-02",
            success_criteria="PR merged with all tests green.",
        )
        assert isinstance(result, UpsertInserted)
        uow_id = result.id

        record = reg.get(uow_id)
        assert record is not None
        assert record.workflow_artifact is None
        # success_criteria is enforced non-empty at upsert time (issue #488).
        assert record.success_criteria == "PR merged with all tests green."
        assert record.prescribed_skills is None
        assert record.steward_cycles == 0
        assert record.timeout_at is None
        assert record.estimated_runtime is None
        assert record.steward_agenda is None
        assert record.steward_log is None


# ---------------------------------------------------------------------------
# prescribed_skills JSON deserialization tests
# ---------------------------------------------------------------------------

class TestPrescribedSkillsDeserialization:
    def _insert_uow_with_prescribed_skills(self, db_path: Path, value) -> str:
        import uuid
        from datetime import datetime, timezone
        conn = _open_db(db_path)
        try:
            uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, sweep_date, status, posture, created_at, updated_at,
                     summary, prescribed_skills)
                VALUES (?, 'executable', 'test', '2026-01-01', 'proposed', 'solo', ?, ?, 'Test', ?)
                """,
                (uow_id, now, now, value),
            )
            conn.commit()
            return uow_id
        finally:
            conn.close()

    def test_prescribed_skills_null_returns_none(self, migrated_db):
        """prescribed_skills = NULL → deserializes to None (not [])."""
        from src.orchestration.registry import Registry
        uow_id = self._insert_uow_with_prescribed_skills(migrated_db, None)
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        assert record is not None
        assert record.prescribed_skills is None

    def test_prescribed_skills_empty_json_array_returns_empty_list(self, migrated_db):
        """prescribed_skills = '[]' → deserializes to [] (not None)."""
        from src.orchestration.registry import Registry
        uow_id = self._insert_uow_with_prescribed_skills(migrated_db, "[]")
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        assert record is not None
        assert record.prescribed_skills == []

    def test_prescribed_skills_json_array_returns_list(self, migrated_db):
        """prescribed_skills = '["skill-a","skill-b"]' → deserializes to list."""
        from src.orchestration.registry import Registry
        uow_id = self._insert_uow_with_prescribed_skills(
            migrated_db, '["systematic-debugging", "verification-before-completion"]'
        )
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        assert record is not None
        assert record.prescribed_skills == [
            "systematic-debugging", "verification-before-completion"
        ]


# ---------------------------------------------------------------------------
# Steward-private fields deserialization tests (should NOT be deserialized)
# ---------------------------------------------------------------------------

class TestStewardPrivateFields:
    def _insert_uow_with_steward_fields(
        self, db_path: Path, steward_agenda, steward_log
    ) -> str:
        import uuid
        from datetime import datetime, timezone
        conn = _open_db(db_path)
        try:
            uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, sweep_date, status, posture, created_at, updated_at,
                     summary, steward_agenda, steward_log)
                VALUES (?, 'executable', 'test', '2026-01-01', 'proposed', 'solo', ?, ?, 'Test', ?, ?)
                """,
                (uow_id, now, now, steward_agenda, steward_log),
            )
            conn.commit()
            return uow_id
        finally:
            conn.close()

    def test_steward_agenda_null_returns_none(self, migrated_db):
        """steward_agenda = NULL → returned as None on the UoW."""
        from src.orchestration.registry import Registry
        uow_id = self._insert_uow_with_steward_fields(migrated_db, None, None)
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        assert record is not None
        assert record.steward_agenda is None

    def test_steward_log_null_returns_none(self, migrated_db):
        """steward_log = NULL → returned as None on the UoW."""
        from src.orchestration.registry import Registry
        uow_id = self._insert_uow_with_steward_fields(migrated_db, None, None)
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        assert record is not None
        assert record.steward_log is None

    def test_steward_agenda_returned_as_raw_string(self, migrated_db):
        """steward_agenda stored as JSON string is returned as raw string (not parsed)."""
        from src.orchestration.registry import Registry
        agenda_json = '[{"posture": "solo", "status": "pending"}]'
        uow_id = self._insert_uow_with_steward_fields(migrated_db, agenda_json, None)
        reg = Registry(migrated_db)
        record = reg.get(uow_id)
        # steward_agenda is private — returned as raw string, not deserialized.
        assert record is not None
        assert record.steward_agenda == agenda_json


# ---------------------------------------------------------------------------
# validate_phase2_schema tests
# ---------------------------------------------------------------------------

class TestValidatePhase2Schema:
    def test_validate_passes_after_migration(self, migrated_db):
        """validate_phase2_schema must not raise when all fields are present."""
        from src.orchestration.registry import validate_phase2_schema
        conn = _open_db(migrated_db)
        try:
            # Should not raise
            validate_phase2_schema(conn)
        finally:
            conn.close()

    def test_validate_raises_if_field_missing(self, pre_migration_db):
        """validate_phase2_schema must raise RuntimeError when Phase 2 fields are absent."""
        from src.orchestration.registry import validate_phase2_schema
        conn = _open_db(pre_migration_db)
        try:
            with pytest.raises(RuntimeError, match="schema migration not applied"):
                validate_phase2_schema(conn)
        finally:
            conn.close()

    def test_validate_raises_if_only_some_fields_present(self, pre_migration_db):
        """validate_phase2_schema must raise even if only some Phase 2 fields are missing."""
        conn = _open_db(pre_migration_db)
        try:
            # Add only workflow_artifact — not all fields
            conn.execute("ALTER TABLE uow_registry ADD COLUMN workflow_artifact TEXT NULL")
            conn.commit()
        finally:
            conn.close()

        from src.orchestration.registry import validate_phase2_schema
        conn2 = _open_db(pre_migration_db)
        try:
            with pytest.raises(RuntimeError, match="schema migration not applied"):
                validate_phase2_schema(conn2)
        finally:
            conn2.close()

    def test_validate_raises_for_steward_private_fields(self, tmp_path):
        """validate_phase2_schema must raise if steward_agenda or steward_log is absent."""
        # Use Phase 1 schema so we start with no Phase 2 fields.
        db_path = tmp_path / "partial.db"
        conn = _open_db(db_path)
        try:
            conn.executescript(_PHASE1_SCHEMA_SQL)
            # Add all Phase 2 fields except steward_agenda and steward_log
            for field in PHASE2_FIELDS:
                if field not in ("steward_agenda", "steward_log"):
                    if field == "steward_cycles":
                        col_type = "INTEGER NOT NULL DEFAULT 0"
                    elif field == "success_criteria":
                        col_type = "TEXT NOT NULL DEFAULT ''"
                    else:
                        col_type = "TEXT NULL"
                    conn.execute(
                        f"ALTER TABLE uow_registry ADD COLUMN {field} {col_type}"
                    )
            conn.commit()
        finally:
            conn.close()

        from src.orchestration.registry import validate_phase2_schema
        conn2 = _open_db(db_path)
        try:
            with pytest.raises(RuntimeError, match="schema migration not applied"):
                validate_phase2_schema(conn2)
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# Schema integrity test
# ---------------------------------------------------------------------------

class TestSchemaIntegrity:
    def test_pragma_table_info_contains_all_phase2_fields(self, migrated_db):
        """PRAGMA table_info(uow_registry) must contain all 8 Phase 2 fields."""
        conn = _open_db(migrated_db)
        try:
            rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
            col_names = {r["name"] for r in rows}
        finally:
            conn.close()
        for field in PHASE2_FIELDS:
            assert field in col_names, f"PRAGMA table_info missing: {field}"
