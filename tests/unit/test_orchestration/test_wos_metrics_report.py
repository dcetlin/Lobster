"""
Unit tests for src.orchestration.wos_metrics_report.

Tests verify:
- build_report_data() returns the expected top-level structure with all keys
- build_report_data() correctly sets the `since` field
- render_text() produces non-empty output containing all section headers
- render_text() handles missing/empty section data gracefully
- JSON output structure matches build_report_data() output
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.orchestration.registry import Registry
from src.orchestration.wos_metrics_report import build_report_data, render_text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """Return path to an empty but schema-initialised registry DB."""
    db_path = tmp_path / "registry.db"
    Registry(db_path)
    return db_path


@pytest.fixture
def nonexistent_db(tmp_path: Path) -> Path:
    """Return a path to a DB that does not exist."""
    return tmp_path / "no-such.db"


# ---------------------------------------------------------------------------
# Tests: build_report_data() structure
# ---------------------------------------------------------------------------

EXPECTED_TOP_LEVEL_KEYS = {
    "generated_at",
    "since",
    "prescription_quality",
    "execution_fidelity",
    "diagnostic_accuracy",
    "convergence",
    "complexity",
}


class TestBuildReportData:
    def test_returns_all_expected_top_level_keys(self, empty_db):
        """build_report_data() must return all required keys."""
        report = build_report_data(registry_path=empty_db)
        assert set(report.keys()) == EXPECTED_TOP_LEVEL_KEYS

    def test_nonexistent_db_returns_all_keys(self, nonexistent_db):
        """Even with a missing DB, all keys are present (analytics return empty)."""
        report = build_report_data(registry_path=nonexistent_db)
        assert set(report.keys()) == EXPECTED_TOP_LEVEL_KEYS

    def test_generated_at_is_iso_string(self, empty_db):
        """generated_at must be an ISO-8601 string."""
        report = build_report_data(registry_path=empty_db)
        # Should parse without error
        from datetime import datetime
        datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))

    def test_since_defaults_to_7_days_ago(self, empty_db):
        """When since is not provided, it defaults to 7 days ago (YYYY-MM-DD)."""
        from datetime import datetime, timedelta, timezone
        report = build_report_data(registry_path=empty_db)
        since = report["since"]
        # Should be a YYYY-MM-DD string
        assert len(since) == 10
        assert since.count("-") == 2
        # Should be approximately 7 days ago (within a day of tolerance)
        expected = (datetime.now(tz=timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        assert since == expected

    def test_since_custom_value_preserved(self, empty_db):
        """Explicit since value is passed through to the report."""
        report = build_report_data(registry_path=empty_db, since="2026-01-01")
        assert report["since"] == "2026-01-01"

    def test_prescription_quality_has_expected_structure(self, empty_db):
        """prescription_quality sub-dict must have per_uow and aggregate keys."""
        report = build_report_data(registry_path=empty_db)
        pq = report["prescription_quality"]
        assert "per_uow" in pq
        assert "aggregate" in pq

    def test_execution_fidelity_has_expected_structure(self, empty_db):
        """execution_fidelity sub-dict must have per_uow and aggregate keys."""
        report = build_report_data(registry_path=empty_db)
        ef = report["execution_fidelity"]
        assert "per_uow" in ef
        assert "aggregate" in ef

    def test_diagnostic_accuracy_has_expected_structure(self, empty_db):
        """diagnostic_accuracy sub-dict must have per_uow and aggregate keys."""
        report = build_report_data(registry_path=empty_db)
        da = report["diagnostic_accuracy"]
        assert "per_uow" in da
        assert "aggregate" in da

    def test_convergence_has_expected_structure(self, empty_db):
        """convergence sub-dict must have per_uow and aggregate keys."""
        report = build_report_data(registry_path=empty_db)
        cv = report["convergence"]
        assert "per_uow" in cv
        assert "aggregate" in cv

    def test_complexity_has_expected_structure(self, empty_db):
        """complexity sub-dict must have per_uow and aggregate keys."""
        report = build_report_data(registry_path=empty_db)
        cx = report["complexity"]
        assert "per_uow" in cx
        assert "aggregate" in cx

    def test_empty_db_produces_empty_per_uow_lists(self, empty_db):
        """With no data, all per_uow sections are empty lists."""
        report = build_report_data(registry_path=empty_db)
        assert report["prescription_quality"]["per_uow"] == []
        assert report["execution_fidelity"]["per_uow"] == []
        assert report["diagnostic_accuracy"]["per_uow"] == []
        assert report["convergence"]["per_uow"] == []
        assert report["complexity"]["per_uow"] == []


# ---------------------------------------------------------------------------
# Tests: render_text()
# ---------------------------------------------------------------------------

SECTION_HEADERS = [
    "Prescription Quality",
    "Execution Fidelity",
    "Diagnostic Accuracy",
    "Convergence",
    "Complexity by Register",
]


class TestRenderText:
    def test_output_is_non_empty(self, empty_db):
        """render_text() must return a non-empty string."""
        report = build_report_data(registry_path=empty_db)
        output = render_text(report)
        assert len(output) > 0

    def test_output_contains_all_section_headers(self, empty_db):
        """Each analytics section must appear by name in the text output."""
        report = build_report_data(registry_path=empty_db)
        output = render_text(report)
        for header in SECTION_HEADERS:
            assert header in output, f"Section header '{header}' missing from output"

    def test_output_contains_generated_at(self, empty_db):
        """Report header must include the generated_at timestamp."""
        report = build_report_data(registry_path=empty_db)
        output = render_text(report)
        assert report["generated_at"] in output

    def test_output_contains_since_date(self, empty_db):
        """Report header must include the since date."""
        report = build_report_data(registry_path=empty_db, since="2026-03-01")
        output = render_text(report)
        assert "2026-03-01" in output

    def test_handles_none_aggregate_values_gracefully(self, nonexistent_db):
        """Missing DB → None rates → 'n/a' appears in output, no exceptions."""
        report = build_report_data(registry_path=nonexistent_db)
        output = render_text(report)
        assert "n/a" in output
        # No exception raised — the function returns a string

    def test_render_text_with_data_includes_metrics(self, tmp_path):
        """With a populated UoW, numeric metrics appear in text output."""
        db_path = tmp_path / "registry.db"
        Registry(db_path)
        # Insert a done UoW with completed_at for convergence section
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, source, status, summary, created_at, updated_at,
                 steward_cycles, steward_log, success_criteria, completed_at, register)
            VALUES (?, ?, 'done', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "uow-test", "github:issue/1", "test uow",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                2, None, "", "2026-01-01T02:00:00+00:00", "operational",
            ),
        )
        conn.commit()
        conn.close()
        report = build_report_data(registry_path=db_path)
        output = render_text(report)
        # The operational register block should appear
        assert "operational" in output

    def test_json_output_is_valid_and_contains_all_keys(self, empty_db):
        """JSON serialisation of build_report_data() output is valid."""
        report = build_report_data(registry_path=empty_db)
        serialised = json.dumps(report)
        parsed = json.loads(serialised)
        assert set(parsed.keys()) == EXPECTED_TOP_LEVEL_KEYS
