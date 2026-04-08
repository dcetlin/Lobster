"""
Unit tests for src.orchestration.analytics.prescription_quality_summary().

Tests use an in-memory SQLite DB initialised via the Registry class (same
pattern as test_audit_queries.py). Steward log entries are injected as raw
INSERT statements to avoid importing the full steward module.

Coverage:
- Empty DB (no UoWs): returns empty per_uow, correct data_gap note
- UoWs present but no prescription events: data_gap note, zero counts
- Single UoW with llm+fallback mix: correct per-uow and aggregate counts
- pct_llm / pct_fallback calculation
- avg_cycles_to_done across done UoWs (non-done UoWs excluded)
- Missing/corrupted JSON lines in steward_log: gracefully skipped
- Non-existent DB path: returns data_gap note, no exception
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.orchestration.registry import Registry
from src.orchestration.analytics import (
    prescription_quality_summary,
    convergence_metrics,
    diagnostic_accuracy,
    execution_fidelity_summary,
    diagnostic_accuracy_summary,
    convergence_summary,
    complexity_appropriateness_summary,
    _CONVERGENCE_SCORE_THRESHOLD,
    _STALL_TAIL_LENGTH,
    _OPERATIONAL_COMPLEXITY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> Path:
    """Create a fresh registry DB via Registry (applies schema)."""
    db_path = tmp_path / "registry.db"
    Registry(db_path)
    return db_path


def _insert_uow(
    db_path: Path,
    *,
    uow_id: str,
    summary: str = "test uow",
    status: str = "pending",
    steward_cycles: int = 0,
    steward_log: str | None = None,
) -> None:
    """Insert a minimal UoW row for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, source, status, summary, created_at, updated_at,
             steward_cycles, steward_log, success_criteria)
        VALUES (?, ?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00',
                ?, ?, '')
        """,
        (uow_id, "github:issue/1", status, summary, steward_cycles, steward_log),
    )
    conn.commit()
    conn.close()


def _make_log(*events: dict) -> str:
    """Build a newline-delimited JSON steward_log string."""
    return "\n".join(json.dumps(e) for e in events)


def _prescription_event(path: str, cycles: int = 0, reentry: bool = False) -> dict:
    return {
        "event": "reentry_prescription" if reentry else "prescription",
        "steward_cycles": cycles,
        "prescription_path": path,
    }


# ---------------------------------------------------------------------------
# Tests: missing / empty DB
# ---------------------------------------------------------------------------

class TestMissingDB:
    def test_nonexistent_path_returns_data_gap(self, tmp_path):
        result = prescription_quality_summary(
            registry_path=tmp_path / "does_not_exist.db"
        )
        assert result["per_uow"] == []
        assert result["data_gap"] is not None
        assert "not found" in result["data_gap"].lower()

    def test_nonexistent_path_aggregate_zeros(self, tmp_path):
        result = prescription_quality_summary(
            registry_path=tmp_path / "does_not_exist.db"
        )
        agg = result["aggregate"]
        assert agg["total_uows"] == 0
        assert agg["total_prescriptions"] == 0
        assert agg["pct_llm"] is None
        assert agg["avg_cycles_to_done"] is None


class TestEmptyRegistry:
    def test_empty_db_has_no_per_uow(self, tmp_path):
        db_path = _init_db(tmp_path)
        result = prescription_quality_summary(registry_path=db_path)
        assert result["per_uow"] == []

    def test_empty_db_data_gap_mentions_no_uows(self, tmp_path):
        db_path = _init_db(tmp_path)
        result = prescription_quality_summary(registry_path=db_path)
        assert result["data_gap"] is not None
        assert "no uow" in result["data_gap"].lower()


# ---------------------------------------------------------------------------
# Tests: UoWs present but no prescription events
# ---------------------------------------------------------------------------

class TestNoPrescritionEvents:
    def test_uows_without_prescription_events_data_gap(self, tmp_path):
        db_path = _init_db(tmp_path)
        _insert_uow(db_path, uow_id="uow-1", steward_log=None)
        _insert_uow(db_path, uow_id="uow-2", steward_log="")

        result = prescription_quality_summary(registry_path=db_path)

        assert result["aggregate"]["total_uows"] == 2
        assert result["aggregate"]["uows_with_data"] == 0
        assert result["aggregate"]["total_prescriptions"] == 0
        assert result["data_gap"] is not None

    def test_uow_with_non_prescription_events_excluded(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(
            {"event": "diagnosis", "steward_cycles": 0},
            {"event": "steward_closure", "steward_cycles": 1},
        )
        _insert_uow(db_path, uow_id="uow-1", steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        assert result["per_uow"][0]["prescription_paths"] == []
        assert result["aggregate"]["total_prescriptions"] == 0


# ---------------------------------------------------------------------------
# Tests: single UoW with prescription data
# ---------------------------------------------------------------------------

class TestSingleUoW:
    def test_llm_only_counts(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(
            _prescription_event("llm", cycles=0),
            _prescription_event("llm", cycles=1, reentry=True),
        )
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=2, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        rec = result["per_uow"][0]
        assert rec["llm_count"] == 2
        assert rec["fallback_count"] == 0
        assert rec["prescription_paths"] == ["llm", "llm"]

    def test_fallback_only_counts(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("fallback", cycles=0))
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=1, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        rec = result["per_uow"][0]
        assert rec["llm_count"] == 0
        assert rec["fallback_count"] == 1

    def test_mixed_paths(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(
            _prescription_event("llm", cycles=0),
            _prescription_event("fallback", cycles=1, reentry=True),
            _prescription_event("llm", cycles=2, reentry=True),
        )
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=3, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        rec = result["per_uow"][0]
        assert rec["prescription_paths"] == ["llm", "fallback", "llm"]
        assert rec["llm_count"] == 2
        assert rec["fallback_count"] == 1

    def test_per_uow_fields_populated(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(
            db_path, uow_id="uow-abc", summary="My test uow",
            status="done", steward_cycles=1, steward_log=log
        )

        result = prescription_quality_summary(registry_path=db_path)

        rec = result["per_uow"][0]
        assert rec["id"] == "uow-abc"
        assert rec["summary"] == "My test uow"
        assert rec["status"] == "done"
        assert rec["steward_cycles"] == 1


# ---------------------------------------------------------------------------
# Tests: aggregate calculations
# ---------------------------------------------------------------------------

class TestAggregate:
    def test_pct_llm_and_fallback(self, tmp_path):
        db_path = _init_db(tmp_path)
        # 3 llm, 1 fallback → 75% / 25%
        log = _make_log(
            _prescription_event("llm"),
            _prescription_event("llm", reentry=True),
            _prescription_event("llm", reentry=True),
            _prescription_event("fallback", reentry=True),
        )
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=4, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        agg = result["aggregate"]
        assert agg["total_prescriptions"] == 4
        assert agg["llm_prescriptions"] == 3
        assert agg["fallback_prescriptions"] == 1
        assert agg["pct_llm"] == 75.0
        assert agg["pct_fallback"] == 25.0

    def test_avg_cycles_to_done_excludes_non_done(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-done", status="done",
                    steward_cycles=3, steward_log=log)
        _insert_uow(db_path, uow_id="uow-active", status="active",
                    steward_cycles=10, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        # Only the done UoW's cycles count
        assert result["aggregate"]["avg_cycles_to_done"] == 3.0

    def test_avg_cycles_to_done_none_when_no_done_uows(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-1", status="active",
                    steward_cycles=5, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        assert result["aggregate"]["avg_cycles_to_done"] is None

    def test_multiple_done_uows_average(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-1", status="done",
                    steward_cycles=2, steward_log=log)
        _insert_uow(db_path, uow_id="uow-2", status="done",
                    steward_cycles=4, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        assert result["aggregate"]["avg_cycles_to_done"] == 3.0

    def test_uows_with_data_count(self, tmp_path):
        db_path = _init_db(tmp_path)
        log_with = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-1", steward_log=log_with)
        _insert_uow(db_path, uow_id="uow-2", steward_log=None)  # no data

        result = prescription_quality_summary(registry_path=db_path)

        assert result["aggregate"]["total_uows"] == 2
        assert result["aggregate"]["uows_with_data"] == 1


# ---------------------------------------------------------------------------
# Tests: malformed steward_log
# ---------------------------------------------------------------------------

class TestMalformedLog:
    def test_invalid_json_lines_skipped(self, tmp_path):
        db_path = _init_db(tmp_path)
        # Mix valid and invalid lines
        log = (
            '{"event": "prescription", "prescription_path": "llm"}\n'
            'this is not json\n'
            '{"event": "prescription", "prescription_path": "fallback"}\n'
            '{broken\n'
        )
        _insert_uow(db_path, uow_id="uow-1", steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        rec = result["per_uow"][0]
        # Only the two valid lines with known prescription_path values
        assert rec["prescription_paths"] == ["llm", "fallback"]

    def test_missing_prescription_path_key_skipped(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(
            {"event": "prescription"},             # no prescription_path key
            {"event": "prescription", "prescription_path": "llm"},
            {"event": "prescription", "prescription_path": "unknown_value"},  # invalid value
        )
        _insert_uow(db_path, uow_id="uow-1", steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        # Only the one valid "llm" entry
        assert result["per_uow"][0]["prescription_paths"] == ["llm"]

    def test_empty_log_string_produces_empty_paths(self, tmp_path):
        db_path = _init_db(tmp_path)
        _insert_uow(db_path, uow_id="uow-1", steward_log="   \n  \n  ")

        result = prescription_quality_summary(registry_path=db_path)

        assert result["per_uow"][0]["prescription_paths"] == []


# ---------------------------------------------------------------------------
# Tests: data_gap note logic
# ---------------------------------------------------------------------------

class TestDataGapNote:
    def test_no_data_gap_when_sufficient_data(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(
            _prescription_event("llm"),
            _prescription_event("fallback", reentry=True),
            _prescription_event("llm", reentry=True),
        )
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=3, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        assert result["data_gap"] is None

    def test_data_gap_when_only_one_prescription(self, tmp_path):
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-1", steward_cycles=1, steward_log=log)

        result = prescription_quality_summary(registry_path=db_path)

        # Only 1 prescription — below the sparse threshold of 3
        assert result["data_gap"] is not None
        assert "1 prescription" in result["data_gap"]


# ---------------------------------------------------------------------------
# Helpers for convergence / diagnostic tests
# ---------------------------------------------------------------------------

def _trace_injection_event(score: float, command: str = "proceed") -> dict:
    """Build a trace_injection steward_log entry with a gate_score."""
    return {
        "event": "trace_injection",
        "gate_score": {"score": score, "command": command},
    }


def _insert_audit(
    db_path: Path,
    *,
    uow_id: str,
    event: str,
    ts: str = "2026-01-01T10:00:00+00:00",
    note: str | None = None,
) -> None:
    """Insert a raw audit_log entry for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) VALUES (?,?,?,?,?,?,?)",
        (ts, uow_id, event, None, None, None, note),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests: convergence_metrics
# ---------------------------------------------------------------------------

class TestConvergenceMetrics:
    def test_empty_db_returns_zero_aggregate(self, tmp_path):
        db_path = _init_db(tmp_path)
        result = convergence_metrics(registry_path=db_path)
        assert result["per_uow"] == []
        assert result["aggregate"]["total_tracked"] == 0
        assert result["aggregate"]["convergence_rate"] is None
        assert result["aggregate"]["avg_cycles_to_converge"] is None
        assert result["aggregate"]["stalled_count"] == 0

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = convergence_metrics(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []
        assert result["aggregate"]["total_tracked"] == 0

    def test_uow_with_no_trace_events_not_tracked(self, tmp_path):
        """UoWs with no gate scores produce no tracked entries in aggregate."""
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_uow(db_path, uow_id="uow-1", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        # per_uow has an entry but score_trajectory is empty, so not tracked
        assert result["aggregate"]["total_tracked"] == 0
        assert result["per_uow"][0]["score_trajectory"] == []

    def test_converging_uow_detected(self, tmp_path):
        """UoW whose final score >= threshold is marked converged."""
        db_path = _init_db(tmp_path)
        log = _make_log(
            _trace_injection_event(0.5),
            _trace_injection_event(0.7),
            _trace_injection_event(_CONVERGENCE_SCORE_THRESHOLD),
        )
        _insert_uow(db_path, uow_id="uow-conv", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        rec = result["per_uow"][0]
        assert rec["converged"] is True
        assert rec["cycles_to_converge"] == 3
        assert rec["score_delta"] == round(_CONVERGENCE_SCORE_THRESHOLD - 0.5, 4)

    def test_done_status_implies_converged_even_without_scores(self, tmp_path):
        """A UoW with status='done' is converged regardless of final score."""
        db_path = _init_db(tmp_path)
        # No trace_injection events but status is done
        _insert_uow(db_path, uow_id="uow-done", status="done", steward_log="")
        result = convergence_metrics(registry_path=db_path)
        rec = result["per_uow"][0]
        assert rec["converged"] is True
        assert rec["score_trajectory"] == []
        # Not tracked in aggregate because no score data
        assert result["aggregate"]["total_tracked"] == 0

    def test_non_converging_uow(self, tmp_path):
        """UoW whose final score < threshold and status != done is not converged."""
        db_path = _init_db(tmp_path)
        log = _make_log(
            _trace_injection_event(0.3),
            _trace_injection_event(0.5),
            _trace_injection_event(0.6),
        )
        _insert_uow(db_path, uow_id="uow-nc", status="active", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        rec = result["per_uow"][0]
        assert rec["converged"] is False
        assert rec["cycles_to_converge"] is None
        agg = result["aggregate"]
        assert agg["total_tracked"] == 1
        assert agg["convergence_rate"] == 0.0

    def test_stalled_uow_detected(self, tmp_path):
        """UoW with >= _STALL_TAIL_LENGTH consecutive non-improving tail scores is stalled."""
        db_path = _init_db(tmp_path)
        # Build a trajectory where the last _STALL_TAIL_LENGTH scores don't improve
        improving = [_trace_injection_event(0.3), _trace_injection_event(0.5)]
        stall_scores = [_trace_injection_event(0.5)] * _STALL_TAIL_LENGTH
        log = _make_log(*improving, *stall_scores)
        _insert_uow(db_path, uow_id="uow-stall", status="active", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        assert result["aggregate"]["stalled_count"] == 1

    def test_non_stalled_uow_not_counted(self, tmp_path):
        """UoW with improving tail is not stalled."""
        db_path = _init_db(tmp_path)
        log = _make_log(
            _trace_injection_event(0.3),
            _trace_injection_event(0.4),
            _trace_injection_event(0.6),
            _trace_injection_event(0.85),
        )
        _insert_uow(db_path, uow_id="uow-ok", status="active", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        assert result["aggregate"]["stalled_count"] == 0

    def test_aggregate_convergence_rate(self, tmp_path):
        """Convergence rate = converged UoWs / total tracked UoWs."""
        db_path = _init_db(tmp_path)
        # One converging UoW
        log_conv = _make_log(_trace_injection_event(_CONVERGENCE_SCORE_THRESHOLD))
        _insert_uow(db_path, uow_id="uow-conv", steward_log=log_conv)
        # One non-converging UoW
        log_nc = _make_log(_trace_injection_event(0.4))
        _insert_uow(db_path, uow_id="uow-nc", status="active", steward_log=log_nc)
        result = convergence_metrics(registry_path=db_path)
        agg = result["aggregate"]
        assert agg["total_tracked"] == 2
        assert agg["convergence_rate"] == 0.5

    def test_score_delta_none_for_single_score(self, tmp_path):
        """score_delta is None when there is only one gate score."""
        db_path = _init_db(tmp_path)
        log = _make_log(_trace_injection_event(0.7))
        _insert_uow(db_path, uow_id="uow-1", steward_log=log)
        result = convergence_metrics(registry_path=db_path)
        assert result["per_uow"][0]["score_delta"] is None


# ---------------------------------------------------------------------------
# Tests: diagnostic_accuracy
# ---------------------------------------------------------------------------

class TestDiagnosticAccuracy:
    def test_empty_db_returns_zero_summary(self, tmp_path):
        db_path = _init_db(tmp_path)
        result = diagnostic_accuracy(registry_path=db_path)
        assert result["per_uow"] == []
        assert result["summary"]["total_diagnoses"] == 0
        assert result["summary"]["followed_by_success"] == 0
        assert result["summary"]["followed_by_failure"] == 0
        assert result["summary"]["pending"] == 0
        assert result["summary"]["success_rate"] is None

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = diagnostic_accuracy(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []
        assert result["summary"]["total_diagnoses"] == 0

    def test_diagnosis_followed_by_success(self, tmp_path):
        """UoW with a diagnosis event and execution_complete outcome."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T11:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        assert result["summary"]["total_diagnoses"] == 1
        assert result["summary"]["followed_by_success"] == 1
        assert result["summary"]["followed_by_failure"] == 0
        assert result["summary"]["pending"] == 0
        assert result["summary"]["success_rate"] == 1.0

    def test_diagnosis_followed_by_failure(self, tmp_path):
        """UoW with a diagnosis event and execution_failed outcome."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="steward_prescription",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_failed",
                      ts="2026-01-01T11:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        assert result["summary"]["followed_by_failure"] == 1
        assert result["summary"]["followed_by_success"] == 0
        assert result["summary"]["success_rate"] == 0.0

    def test_pending_uow_no_terminal_outcome(self, tmp_path):
        """UoW with diagnosis events but no terminal outcome is pending."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                      ts="2026-01-01T10:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        assert result["summary"]["pending"] == 1
        assert result["summary"]["success_rate"] is None
        per = result["per_uow"][0]
        assert per["outcome"] is None

    def test_multiple_diagnosis_events_same_uow(self, tmp_path):
        """Multiple diagnosis events for a single UoW are counted in total_diagnoses."""
        db_path = _init_db(tmp_path)
        for i in range(3):
            _insert_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                          ts=f"2026-01-01T{10+i:02d}:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T15:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        # 3 diagnosis events across 1 UoW
        assert result["summary"]["total_diagnoses"] == 3
        assert result["summary"]["followed_by_success"] == 1
        assert len(result["per_uow"]) == 1

    def test_mixed_outcomes(self, tmp_path):
        """Two UoWs: one success, one failure — success_rate = 0.5."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-success", event="steward_diagnosis",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-success", event="execution_complete",
                      ts="2026-01-01T11:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-fail", event="steward_diagnosis",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-fail", event="execution_failed",
                      ts="2026-01-01T11:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        assert result["summary"]["followed_by_success"] == 1
        assert result["summary"]["followed_by_failure"] == 1
        assert result["summary"]["success_rate"] == 0.5

    def test_latest_terminal_outcome_wins(self, tmp_path):
        """If a UoW has both failure and complete events, the latest one counts."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_failed",
                      ts="2026-01-01T11:00:00+00:00")
        # Re-run completed successfully after failure
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T12:00:00+00:00")
        result = diagnostic_accuracy(registry_path=db_path)
        # Latest event is execution_complete → success
        assert result["summary"]["followed_by_success"] == 1
        assert result["summary"]["followed_by_failure"] == 0


# ---------------------------------------------------------------------------
# Tests: execution_fidelity_summary
# ---------------------------------------------------------------------------

class TestExecutionFidelitySummary:
    def test_empty_db_returns_zero_aggregate(self, tmp_path):
        """Empty audit_log produces zero totals and None rates."""
        db_path = _init_db(tmp_path)
        result = execution_fidelity_summary(registry_path=db_path)
        assert result["per_uow"] == []
        agg = result["aggregate"]
        assert agg["total_executions"] == 0
        assert agg["success_rate"] is None
        assert agg["failure_rate"] is None
        assert agg["re_diagnosis_rate"] is None

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = execution_fidelity_summary(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []
        assert result["aggregate"]["total_executions"] == 0

    def test_single_successful_execution(self, tmp_path):
        """UoW with one dispatch and one complete: success_rate = 1.0."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="executor_dispatch",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T11:00:00+00:00")
        result = execution_fidelity_summary(registry_path=db_path)
        assert result["aggregate"]["total_executions"] == 1
        assert result["aggregate"]["success_rate"] == 1.0
        assert result["aggregate"]["failure_rate"] == 0.0
        per = result["per_uow"][0]
        assert per["uow_id"] == "uow-1"
        assert per["final_outcome"] == "execution_complete"
        assert per["re_diagnosis_occurred"] is False

    def test_single_failed_execution_with_rediagnosis(self, tmp_path):
        """Failure followed by steward_diagnosis is flagged as re_diagnosis."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="execution_failed",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                      ts="2026-01-01T11:00:00+00:00")
        result = execution_fidelity_summary(registry_path=db_path)
        agg = result["aggregate"]
        assert agg["total_executions"] == 1
        assert agg["failure_rate"] == 1.0
        assert agg["re_diagnosis_rate"] == 1.0
        per = result["per_uow"][0]
        assert per["re_diagnosis_occurred"] is True

    def test_mixed_outcomes_two_uows(self, tmp_path):
        """One success, one failure — success_rate = 0.5."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-ok", event="execution_complete",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-fail", event="execution_failed",
                      ts="2026-01-01T10:00:00+00:00")
        result = execution_fidelity_summary(registry_path=db_path)
        agg = result["aggregate"]
        assert agg["total_executions"] == 2
        assert agg["success_rate"] == 0.5
        assert agg["failure_rate"] == 0.5
        assert agg["re_diagnosis_rate"] == 0.0

    def test_no_rediagnosis_without_failure(self, tmp_path):
        """Successful UoW with subsequent steward_diagnosis not flagged as re_diagnosis."""
        db_path = _init_db(tmp_path)
        # No prior failure for this UoW — re_diagnosis_occurred should be False
        _insert_audit(db_path, uow_id="uow-ok", event="execution_complete",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-ok", event="steward_diagnosis",
                      ts="2026-01-01T11:00:00+00:00")
        result = execution_fidelity_summary(registry_path=db_path)
        # Since uow-ok had no failure, it should NOT be flagged
        assert result["aggregate"]["re_diagnosis_rate"] == 0.0


# ---------------------------------------------------------------------------
# Tests: diagnostic_accuracy_summary
# ---------------------------------------------------------------------------

def _insert_uow_with_log(
    db_path: Path,
    uow_id: str,
    status: str = "done",
    steward_log: str = "",
) -> None:
    """Insert a UoW with a steward_log for diagnostic_accuracy_summary tests."""
    _insert_uow(db_path, uow_id=uow_id, status=status, steward_log=steward_log)


class TestDiagnosticAccuracySummary:
    def test_empty_db_returns_zero_aggregate(self, tmp_path):
        """Empty DB: zero totals, None success rate."""
        db_path = _init_db(tmp_path)
        result = diagnostic_accuracy_summary(registry_path=db_path)
        assert result["per_uow"] == []
        agg = result["aggregate"]
        assert agg["total_diagnosed"] == 0
        assert agg["successful_first_attempt_count"] == 0
        assert agg["first_attempt_success_rate"] is None

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = diagnostic_accuracy_summary(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []

    def test_first_attempt_success_when_complete_precedes_failure(self, tmp_path):
        """execution_complete before any execution_failed → first_attempt_success=True."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="prescription",
                      ts="2026-01-01T09:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T10:00:00+00:00")
        result = diagnostic_accuracy_summary(registry_path=db_path)
        agg = result["aggregate"]
        assert agg["total_diagnosed"] == 1
        assert agg["successful_first_attempt_count"] == 1
        assert agg["first_attempt_success_rate"] == 1.0
        assert result["per_uow"][0]["first_attempt_success"] is True

    def test_first_attempt_failure_when_failed_precedes_complete(self, tmp_path):
        """execution_failed before execution_complete → first_attempt_success=False."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-1", event="reentry_prescription",
                      ts="2026-01-01T09:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_failed",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-1", event="execution_complete",
                      ts="2026-01-01T11:00:00+00:00")
        result = diagnostic_accuracy_summary(registry_path=db_path)
        assert result["aggregate"]["first_attempt_success_rate"] == 0.0
        assert result["per_uow"][0]["first_attempt_success"] is False

    def test_pending_uow_with_no_execution_event(self, tmp_path):
        """UoW with prescription but no execution event yet → None success."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-pending", event="prescription",
                      ts="2026-01-01T09:00:00+00:00")
        result = diagnostic_accuracy_summary(registry_path=db_path)
        agg = result["aggregate"]
        assert agg["total_diagnosed"] == 1
        assert agg["first_attempt_success_rate"] is None
        assert result["per_uow"][0]["first_attempt_success"] is None

    def test_multiple_uows_mixed_outcomes(self, tmp_path):
        """Two UoWs: one first-attempt success, one failure → rate = 0.5."""
        db_path = _init_db(tmp_path)
        _insert_audit(db_path, uow_id="uow-good", event="prescription",
                      ts="2026-01-01T09:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-good", event="execution_complete",
                      ts="2026-01-01T10:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-bad", event="prescription",
                      ts="2026-01-01T09:00:00+00:00")
        _insert_audit(db_path, uow_id="uow-bad", event="execution_failed",
                      ts="2026-01-01T10:00:00+00:00")
        result = diagnostic_accuracy_summary(registry_path=db_path)
        assert result["aggregate"]["first_attempt_success_rate"] == 0.5


# ---------------------------------------------------------------------------
# Tests: convergence_summary
# ---------------------------------------------------------------------------

def _insert_completed_uow(
    db_path: Path,
    uow_id: str,
    steward_cycles: int,
    created_at: str,
    completed_at: str | None,
) -> None:
    """Insert a completed UoW with timing fields for convergence_summary tests."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, source, status, summary, created_at, updated_at,
             steward_cycles, steward_log, success_criteria, completed_at)
        VALUES (?, ?, 'done', ?, ?, ?, ?, '', '', ?)
        """,
        (uow_id, "github:issue/1", "test", created_at, created_at,
         steward_cycles, completed_at),
    )
    conn.commit()
    conn.close()


class TestConvergenceSummary:
    def test_empty_db_returns_zero_aggregate(self, tmp_path):
        """No completed UoWs → all aggregates None."""
        db_path = _init_db(tmp_path)
        result = convergence_summary(registry_path=db_path)
        assert result["per_uow"] == []
        agg = result["aggregate"]
        assert agg["avg_cycles_to_done"] is None
        assert agg["median_cycles"] is None
        assert agg["outlier_uow_ids"] == []

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = convergence_summary(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []

    def test_single_completed_uow(self, tmp_path):
        """Single done UoW: avg/median/max all equal steward_cycles."""
        db_path = _init_db(tmp_path)
        _insert_completed_uow(
            db_path, "uow-1", steward_cycles=3,
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T06:00:00+00:00",
        )
        result = convergence_summary(registry_path=db_path)
        assert len(result["per_uow"]) == 1
        agg = result["aggregate"]
        assert agg["avg_cycles_to_done"] == 3.0
        assert agg["median_cycles"] == 3
        assert agg["max_cycles"] == 3
        assert agg["avg_wall_clock_hours"] == 6.0
        assert agg["outlier_uow_ids"] == []

    def test_outlier_detection(self, tmp_path):
        """UoW with cycles > 2 * median is flagged as outlier."""
        db_path = _init_db(tmp_path)
        # Three normal UoWs at 2 cycles each (median=2)
        for i in range(3):
            _insert_completed_uow(
                db_path, f"uow-{i}", steward_cycles=2,
                created_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T01:00:00+00:00",
            )
        # One outlier at 5 cycles (> 2 * 2 = 4)
        _insert_completed_uow(
            db_path, "uow-outlier", steward_cycles=5,
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T02:00:00+00:00",
        )
        result = convergence_summary(registry_path=db_path)
        assert "uow-outlier" in result["aggregate"]["outlier_uow_ids"]

    def test_no_outlier_when_cycles_within_bounds(self, tmp_path):
        """UoW with cycles = 2 * median is NOT an outlier (must strictly exceed)."""
        db_path = _init_db(tmp_path)
        _insert_completed_uow(
            db_path, "uow-1", steward_cycles=2,
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T01:00:00+00:00",
        )
        _insert_completed_uow(
            db_path, "uow-2", steward_cycles=4,  # exactly 2 * median(2) — not an outlier
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T02:00:00+00:00",
        )
        result = convergence_summary(registry_path=db_path)
        assert result["aggregate"]["outlier_uow_ids"] == []

    def test_wall_clock_hours_computed(self, tmp_path):
        """Wall-clock hours are correctly derived from created_at / completed_at."""
        db_path = _init_db(tmp_path)
        _insert_completed_uow(
            db_path, "uow-1", steward_cycles=1,
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T02:30:00+00:00",
        )
        result = convergence_summary(registry_path=db_path)
        assert result["per_uow"][0]["wall_clock_hours"] == 2.5

    def test_pending_uows_excluded(self, tmp_path):
        """Only done UoWs are counted in convergence_summary."""
        db_path = _init_db(tmp_path)
        # Insert a pending UoW — should not appear
        _insert_uow(db_path, uow_id="uow-pending", status="pending", steward_cycles=5)
        result = convergence_summary(registry_path=db_path)
        assert result["per_uow"] == []


# ---------------------------------------------------------------------------
# Tests: complexity_appropriateness_summary
# ---------------------------------------------------------------------------

def _insert_complexity_uow(
    db_path: Path,
    uow_id: str,
    register: str = "operational",
    uow_type: str = "executable",
    steward_cycles: int = 0,
    steward_log: str = "",
) -> None:
    """Insert a UoW with register/type/cycles/log for complexity tests."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, source, status, summary, created_at, updated_at,
             steward_cycles, steward_log, success_criteria, register, type)
        VALUES (?, ?, 'done', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00',
                ?, ?, '', ?, ?)
        """,
        (uow_id, "github:issue/1", "test", steward_cycles, steward_log, register, uow_type),
    )
    conn.commit()
    conn.close()


class TestComplexityAppropriatenessSummary:
    def test_empty_db_returns_empty_breakdown(self, tmp_path):
        """No UoWs → empty by_register."""
        db_path = _init_db(tmp_path)
        result = complexity_appropriateness_summary(registry_path=db_path)
        assert result["per_uow"] == []
        assert result["aggregate"]["by_register"] == {}

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = complexity_appropriateness_summary(registry_path=tmp_path / "no.db")
        assert result["per_uow"] == []

    def test_single_operational_uow_llm_path(self, tmp_path):
        """Operational UoW with LLM path is correctly classified."""
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_complexity_uow(
            db_path, "uow-1", register="operational", steward_cycles=2, steward_log=log
        )
        result = complexity_appropriateness_summary(registry_path=db_path)
        per = result["per_uow"][0]
        assert per["register"] == "operational"
        assert per["prescription_path"] == "llm"
        assert per["over_complex_flag"] is False
        agg = result["aggregate"]["by_register"]["operational"]
        assert agg["pct_llm"] == 100.0
        assert agg["pct_fallback"] == 0.0

    def test_over_complex_flag_set_for_operational_high_cycles(self, tmp_path):
        """Operational UoW with cycles > _OPERATIONAL_COMPLEXITY_THRESHOLD is flagged."""
        db_path = _init_db(tmp_path)
        _insert_complexity_uow(
            db_path, "uow-heavy", register="operational",
            steward_cycles=_OPERATIONAL_COMPLEXITY_THRESHOLD + 1
        )
        result = complexity_appropriateness_summary(registry_path=db_path)
        per = result["per_uow"][0]
        assert per["over_complex_flag"] is True
        agg = result["aggregate"]["by_register"]["operational"]
        assert agg["over_complex_count"] == 1

    def test_over_complex_not_flagged_at_threshold(self, tmp_path):
        """Cycles == _OPERATIONAL_COMPLEXITY_THRESHOLD is NOT flagged (must strictly exceed)."""
        db_path = _init_db(tmp_path)
        _insert_complexity_uow(
            db_path, "uow-ok", register="operational",
            steward_cycles=_OPERATIONAL_COMPLEXITY_THRESHOLD
        )
        result = complexity_appropriateness_summary(registry_path=db_path)
        assert result["per_uow"][0]["over_complex_flag"] is False

    def test_non_operational_register_not_flagged(self, tmp_path):
        """Non-operational registers with high cycles are NOT flagged as over-complex."""
        db_path = _init_db(tmp_path)
        _insert_complexity_uow(
            db_path, "uow-deep", register="reflective",
            steward_cycles=10  # high cycles OK for non-operational
        )
        result = complexity_appropriateness_summary(registry_path=db_path)
        assert result["per_uow"][0]["over_complex_flag"] is False

    def test_multiple_registers_grouped_separately(self, tmp_path):
        """UoWs in different registers appear in separate by_register buckets."""
        db_path = _init_db(tmp_path)
        log = _make_log(_prescription_event("llm"))
        _insert_complexity_uow(db_path, "uow-op", register="operational", steward_log=log)
        _insert_complexity_uow(db_path, "uow-ref", register="reflective",
                               steward_log=_make_log(_prescription_event("fallback")))
        result = complexity_appropriateness_summary(registry_path=db_path)
        by_reg = result["aggregate"]["by_register"]
        assert "operational" in by_reg
        assert "reflective" in by_reg
        assert by_reg["operational"]["count"] == 1
        assert by_reg["reflective"]["count"] == 1

    def test_uow_with_no_prescription_events_has_unknown_path(self, tmp_path):
        """UoW with no prescription events in steward_log → prescription_path = 'unknown'."""
        db_path = _init_db(tmp_path)
        _insert_complexity_uow(db_path, "uow-blank", register="operational", steward_log="")
        result = complexity_appropriateness_summary(registry_path=db_path)
        assert result["per_uow"][0]["prescription_path"] == "unknown"
