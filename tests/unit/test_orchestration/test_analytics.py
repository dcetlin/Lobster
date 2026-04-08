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
    _CONVERGENCE_SCORE_THRESHOLD,
    _STALL_TAIL_LENGTH,
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
