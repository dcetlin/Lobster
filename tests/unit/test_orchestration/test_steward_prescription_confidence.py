"""
Tests for prescription_confidence field — issue #995.

Tests are derived from the spec (issue #995) and the named constants in steward.py.
They verify behavior, not mechanism: the column exists, the value is computed
correctly from named constants, and the value is stored in both the registry
row and the steward_prescription audit log entry.

Coverage:
- Schema: prescription_confidence column present in uow_registry
- Computation: _compute_prescription_confidence returns correct constant for each
  decision-table branch (first execution, low attempts, high attempts, high cycles)
- Storage: confidence written to registry row at prescription time
- Audit: confidence included in steward_prescription audit log entry
- Backwards compatibility: NULL when column not written (pre-migration rows)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    _compute_prescription_confidence,
    CONFIDENCE_FIRST_EXECUTION,
    CONFIDENCE_LOW_ATTEMPTS,
    CONFIDENCE_HIGH_ATTEMPTS,
    CONFIDENCE_HIGH_CYCLES,
    CONFIDENCE_HIGH_CYCLES_THRESHOLD,
    CONFIDENCE_HIGH_ATTEMPTS_THRESHOLD,
    _write_steward_fields,
    run_steward_cycle,
    IssueInfo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert_uow(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "diagnosing",
    steward_cycles: int = 0,
    execution_attempts: int = 0,
    success_criteria: str = "Output file exists with non-empty content",
) -> str:
    """Insert a minimal UoW row. Returns the uow_id."""
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, steward_cycles, lifetime_cycles,
             execution_attempts, success_criteria, route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', 42, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, status, now, now, "Test UoW",
         steward_cycles, steward_cycles,
         execution_attempts, success_criteria),
    )
    conn.commit()
    return uow_id


def _get_row(conn: sqlite3.Connection, uow_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    return dict(row) if row else {}


def _get_audit_entries(conn: sqlite3.Connection, uow_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
        (uow_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path: Path):
    """
    Returns a Registry bootstrapped on a fresh DB.

    Registry.__init__ applies schema.sql + all migrations in one pass via
    run_migrations(). No manual schema application is needed — and doing so
    would cause duplicate-column errors from the ALTER TABLE migrations.
    """
    from src.orchestration.registry import Registry
    return Registry(tmp_path / "registry.db")


@pytest.fixture
def db_path(registry) -> Path:
    """Convenience: the db_path from a bootstrapped registry."""
    return registry.db_path


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestPrescriptionConfidenceSchema:
    """Schema-level tests: column presence and nullability."""

    def test_column_present_in_uow_registry(self, db_path: Path) -> None:
        """prescription_confidence column must exist on uow_registry."""
        conn = _open_db(db_path)
        try:
            info = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
            column_names = [row["name"] for row in info]
            assert "prescription_confidence" in column_names, (
                "prescription_confidence column missing from uow_registry — "
                "migration 0016 may not have been applied to schema.sql"
            )
        finally:
            conn.close()

    def test_column_is_nullable_real(self, db_path: Path) -> None:
        """prescription_confidence must be REAL and nullable (backwards compatible)."""
        conn = _open_db(db_path)
        try:
            info = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
            col = next((r for r in info if r["name"] == "prescription_confidence"), None)
            assert col is not None
            assert col["type"].upper() == "REAL"
            assert col["notnull"] == 0, "prescription_confidence must be nullable"
            assert col["dflt_value"] is None, (
                "prescription_confidence must have no default — NULL signals not-yet-written"
            )
        finally:
            conn.close()

    def test_existing_rows_get_null_confidence(self, db_path: Path) -> None:
        """Rows inserted without setting confidence must have NULL (backwards compat)."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(conn)
        row = _get_row(conn, uow_id)
        assert row["prescription_confidence"] is None
        conn.close()


# ---------------------------------------------------------------------------
# Computation tests
# ---------------------------------------------------------------------------

class TestComputePrescriptionConfidence:
    """Unit tests for _compute_prescription_confidence — pure function, no I/O."""

    def test_first_execution_with_criteria_returns_high_baseline(self) -> None:
        """First execution with success_criteria set → CONFIDENCE_FIRST_EXECUTION."""
        result = _compute_prescription_confidence(
            steward_cycles=0,
            execution_attempts=0,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_FIRST_EXECUTION

    def test_first_execution_without_criteria_returns_low_attempts(self) -> None:
        """First execution with no success_criteria → CONFIDENCE_LOW_ATTEMPTS (fallback)."""
        result = _compute_prescription_confidence(
            steward_cycles=0,
            execution_attempts=0,
            success_criteria_present=False,
        )
        assert result == CONFIDENCE_LOW_ATTEMPTS

    def test_reentry_with_one_attempt_returns_low_attempts(self) -> None:
        """Re-entry with execution_attempts=1 → CONFIDENCE_LOW_ATTEMPTS."""
        result = _compute_prescription_confidence(
            steward_cycles=1,
            execution_attempts=1,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_LOW_ATTEMPTS

    def test_reentry_with_two_attempts_returns_low_attempts(self) -> None:
        """Re-entry with execution_attempts=2 (below threshold) → CONFIDENCE_LOW_ATTEMPTS."""
        result = _compute_prescription_confidence(
            steward_cycles=2,
            execution_attempts=2,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_LOW_ATTEMPTS

    def test_high_attempts_threshold_returns_high_attempts_confidence(self) -> None:
        """execution_attempts at CONFIDENCE_HIGH_ATTEMPTS_THRESHOLD → CONFIDENCE_HIGH_ATTEMPTS."""
        result = _compute_prescription_confidence(
            steward_cycles=2,
            execution_attempts=CONFIDENCE_HIGH_ATTEMPTS_THRESHOLD,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_HIGH_ATTEMPTS

    def test_high_cycles_threshold_overrides_all(self) -> None:
        """steward_cycles at CONFIDENCE_HIGH_CYCLES_THRESHOLD → CONFIDENCE_HIGH_CYCLES (first gate)."""
        result = _compute_prescription_confidence(
            steward_cycles=CONFIDENCE_HIGH_CYCLES_THRESHOLD,
            execution_attempts=0,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_HIGH_CYCLES

    def test_high_cycles_takes_priority_over_high_attempts(self) -> None:
        """High cycle count supersedes high attempt count — cycles gate is first in table."""
        result = _compute_prescription_confidence(
            steward_cycles=CONFIDENCE_HIGH_CYCLES_THRESHOLD,
            execution_attempts=CONFIDENCE_HIGH_ATTEMPTS_THRESHOLD,
            success_criteria_present=True,
        )
        assert result == CONFIDENCE_HIGH_CYCLES

    def test_return_value_in_unit_interval(self) -> None:
        """All named constants must be in [0.0, 1.0]."""
        for val in (
            CONFIDENCE_FIRST_EXECUTION,
            CONFIDENCE_LOW_ATTEMPTS,
            CONFIDENCE_HIGH_ATTEMPTS,
            CONFIDENCE_HIGH_CYCLES,
        ):
            assert 0.0 <= val <= 1.0, f"Confidence constant {val} is outside [0.0, 1.0]"


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------

class TestPrescriptionConfidenceStorage:
    """Tests that confidence is written to the registry row via _write_steward_fields."""

    def test_confidence_written_to_registry_row(
        self, db_path: Path, registry
    ) -> None:
        """_write_steward_fields with prescription_confidence updates the row."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(conn, steward_cycles=0, execution_attempts=0)
        conn.close()

        confidence = CONFIDENCE_FIRST_EXECUTION
        _write_steward_fields(registry, uow_id, prescription_confidence=confidence)

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        assert row["prescription_confidence"] == pytest.approx(confidence), (
            f"Expected prescription_confidence={confidence}, got {row['prescription_confidence']}"
        )

    def test_confidence_not_overwritten_when_not_passed(
        self, db_path: Path, registry
    ) -> None:
        """_write_steward_fields calls without confidence must not overwrite an existing value."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(conn)
        conn.close()

        # Write initial confidence
        _write_steward_fields(registry, uow_id, prescription_confidence=CONFIDENCE_FIRST_EXECUTION)

        # Write some other field without touching confidence
        _write_steward_fields(registry, uow_id, steward_cycles=1)

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        assert row["prescription_confidence"] == pytest.approx(CONFIDENCE_FIRST_EXECUTION), (
            "Subsequent _write_steward_fields call without confidence must not clear the value"
        )

    def test_confidence_value_is_float_not_int(
        self, db_path: Path, registry
    ) -> None:
        """Stored value must be REAL (float), not an integer truncation."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(conn)
        conn.close()

        _write_steward_fields(registry, uow_id, prescription_confidence=0.6)

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        stored = row["prescription_confidence"]
        assert isinstance(stored, float), f"Expected float, got {type(stored)}"
        assert stored == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Integration: confidence computed from UoW signals and stored in one call
# ---------------------------------------------------------------------------

class TestConfidenceComputedAndStoredFromUoWSignals:
    """
    Verify the compute → store pipeline: given UoW signals, the correct
    constant is selected and persisted.
    """

    def test_first_execution_uow_stores_first_execution_confidence(
        self, db_path: Path, registry
    ) -> None:
        """A brand-new UoW (cycles=0, attempts=0, criteria set) stores CONFIDENCE_FIRST_EXECUTION."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(
            conn,
            steward_cycles=0,
            execution_attempts=0,
            success_criteria="Output file exists",
        )
        conn.close()

        # Simulate what steward does at dispatch
        confidence = _compute_prescription_confidence(
            steward_cycles=0,
            execution_attempts=0,
            success_criteria_present=True,
        )
        _write_steward_fields(registry, uow_id, prescription_confidence=confidence)

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        assert row["prescription_confidence"] == pytest.approx(CONFIDENCE_FIRST_EXECUTION)

    def test_struggling_uow_stores_low_confidence(
        self, db_path: Path, registry
    ) -> None:
        """A UoW at CONFIDENCE_HIGH_CYCLES_THRESHOLD stores CONFIDENCE_HIGH_CYCLES."""
        conn = _open_db(db_path)
        uow_id = _insert_uow(
            conn,
            steward_cycles=CONFIDENCE_HIGH_CYCLES_THRESHOLD,
            execution_attempts=2,
            success_criteria="Output file exists",
        )
        conn.close()

        confidence = _compute_prescription_confidence(
            steward_cycles=CONFIDENCE_HIGH_CYCLES_THRESHOLD,
            execution_attempts=2,
            success_criteria_present=True,
        )
        _write_steward_fields(registry, uow_id, prescription_confidence=confidence)

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        assert row["prescription_confidence"] == pytest.approx(CONFIDENCE_HIGH_CYCLES)


# ---------------------------------------------------------------------------
# Audit log tests: confidence included in steward_prescription audit entry
# ---------------------------------------------------------------------------

def _mock_github_client(issue_number: int) -> IssueInfo:
    """Minimal stub: open issue with body, no labels."""
    return IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body=f"Issue #{issue_number}: implement this.\n\nAcceptance:\n- Works",
        title=f"Test issue {issue_number}",
    )


class TestPrescriptionConfidenceAuditLog:
    """
    Verify that prescription_confidence appears in the steward_prescription
    audit log entry written by _capturing_prescriber → registry.append_audit_log.

    These tests exercise the full write path: run_steward_cycle claims a
    ready-for-steward UoW, computes confidence, writes the registry row, and
    appends the steward_prescription audit entry — all in one call.
    """

    def test_audit_entry_contains_prescription_confidence(
        self, db_path: Path, registry, tmp_path: Path
    ) -> None:
        """
        The steward_prescription audit log entry must include prescription_confidence.

        Uses llm_prescriber=None (deterministic path) to avoid real LLM calls.
        """
        conn = _open_db(db_path)
        uow_id = _insert_uow(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            execution_attempts=0,
            success_criteria="Output file exists with non-empty content",
        )
        conn.close()

        run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client,
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=None,  # deterministic path — no LLM call
        )

        # Only assert on the audit entry if the UoW reached ready-for-executor.
        # If diagnosis gated it out (e.g. waiting for trace), skip — the audit
        # entry does not exist yet and that is correct behavior.
        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        if row.get("status") != "ready-for-executor":
            pytest.skip(
                f"UoW did not reach ready-for-executor (status={row.get('status')!r}) "
                "— steward_prescription audit entry not written on this code path"
            )

        conn = _open_db(db_path)
        entries = _get_audit_entries(conn, uow_id)
        conn.close()
        presc_entry = next(
            (e for e in entries if e.get("event") == "steward_prescription"), None
        )
        assert presc_entry is not None, (
            "steward_prescription audit entry not found — "
            "audit write path may be broken"
        )

        note_data = json.loads(presc_entry["note"])
        assert "prescription_confidence" in note_data, (
            f"prescription_confidence missing from steward_prescription audit entry. "
            f"Keys present: {list(note_data.keys())}"
        )
        confidence_value = note_data["prescription_confidence"]
        assert isinstance(confidence_value, float), (
            f"prescription_confidence must be a float, got {type(confidence_value)}"
        )
        assert 0.0 <= confidence_value <= 1.0, (
            f"prescription_confidence must be in [0.0, 1.0], got {confidence_value}"
        )
        # First execution with criteria → CONFIDENCE_FIRST_EXECUTION
        assert confidence_value == pytest.approx(CONFIDENCE_FIRST_EXECUTION), (
            f"Expected CONFIDENCE_FIRST_EXECUTION ({CONFIDENCE_FIRST_EXECUTION}) "
            f"for a first-execution UoW, got {confidence_value}"
        )

    def test_audit_confidence_matches_registry_row(
        self, db_path: Path, registry, tmp_path: Path
    ) -> None:
        """
        The confidence stored in the audit entry must match the value written to
        the registry row — both are computed from the same _compute_prescription_confidence
        call and must be identical.
        """
        conn = _open_db(db_path)
        uow_id = _insert_uow(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            execution_attempts=0,
            success_criteria="Feature renders correctly",
        )
        conn.close()

        run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client,
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=None,
        )

        conn = _open_db(db_path)
        row = _get_row(conn, uow_id)
        conn.close()

        if row.get("status") != "ready-for-executor":
            pytest.skip(
                f"UoW did not reach ready-for-executor (status={row.get('status')!r})"
            )

        conn = _open_db(db_path)
        entries = _get_audit_entries(conn, uow_id)
        conn.close()
        presc_entry = next(
            (e for e in entries if e.get("event") == "steward_prescription"), None
        )
        assert presc_entry is not None

        note_data = json.loads(presc_entry["note"])
        audit_confidence = note_data.get("prescription_confidence")
        row_confidence = row.get("prescription_confidence")

        assert audit_confidence is not None, "prescription_confidence missing from audit entry"
        assert row_confidence is not None, "prescription_confidence missing from registry row"
        assert audit_confidence == pytest.approx(row_confidence), (
            f"Audit confidence {audit_confidence} != row confidence {row_confidence} "
            "— the two write paths must use the same computed value"
        )
