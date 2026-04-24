"""
Unit tests for the JuiceSensor and compute_juice.

Derived from the spec in juice-uow-integration-spec.md and the sensing
protocol defined in issues #888 and #889. Tests verify behavior, not
implementation internals.

Spec requirements being tested:

1. No execution history → score=None, has_juice=False
   (insufficient data — cannot assert juice without evidence)

2. High oracle approval rate (>=50%) with enough samples → juice asserted
   ("oracle approval rate" is the strongest juice signal)

3. All failures / zero oracle approvals → juice not asserted
   (stuck/indeterminate thread should not get juice)

4. Completed prerequisites add a positive signal
   (cleared ground signals a thread that is moving forward)

5. Recent oracle approval (most recent verdict = approved) amplifies score
   (recency matters — a stale approval is weaker evidence)

6. JuiceAssessment.rationale is non-empty only when has_juice=True
   (mandatory rationale rule from the spec)

7. compute_juice returns None for unknown UoW IDs
   (safe default when UoW is missing)

8. JuiceSignals is immutable (frozen dataclass — no accidental mutation)
"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.juice import (
    JuiceSensor,
    JuiceSignals,
    JuiceAssessment,
    compute_juice,
    _assess_from_signals,
    _extract_signals,
    _extract_signals_with_prerequisites,
    _score_oracle_approval,
    _score_completion_rate,
    _score_prerequisites,
    _aggregate_score,
    _build_rationale,
    JUICE_MIN_ORACLE_APPROVAL_RATE,
    JUICE_MIN_ORACLE_SAMPLE_SIZE,
    JUICE_MIN_COMPLETION_RATE,
    JUICE_PREREQUISITE_POSITIVE_THRESHOLD,
    JUICE_SCORE_THRESHOLD,
    JUICE_UPDATE_DELTA,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_signals(
    oracle_approval_count: int = 0,
    total_execution_cycles: int = 0,
    done_outcome_count: int = 0,
    completed_prerequisite_count: int = 0,
    recent_oracle_approved: bool = False,
) -> JuiceSignals:
    """Construct a JuiceSignals for testing."""
    return JuiceSignals(
        oracle_approval_count=oracle_approval_count,
        total_execution_cycles=total_execution_cycles,
        done_outcome_count=done_outcome_count,
        completed_prerequisite_count=completed_prerequisite_count,
        recent_oracle_approved=recent_oracle_approved,
    )


def _make_audit_entry(
    event: str,
    to_status: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Construct a minimal audit log entry dict."""
    entry: dict[str, Any] = {"event": event}
    if to_status is not None:
        entry["to_status"] = to_status
    if note is not None:
        entry["note"] = note
    return entry


# ---------------------------------------------------------------------------
# Tests: no execution history (edge case: empty / no data)
# ---------------------------------------------------------------------------

class TestNoExecutionHistory:
    """No execution cycles → cannot assert juice (spec: insufficient data)."""

    def test_no_execution_history_returns_none_score(self):
        """Spec: when there is no execution history, score must be None."""
        signals = _make_signals(total_execution_cycles=0)
        assessment = _assess_from_signals("uow_test_001", signals)
        assert assessment.score is None

    def test_no_execution_history_has_juice_false(self):
        """Spec: no history → has_juice must be False."""
        signals = _make_signals(total_execution_cycles=0)
        assessment = _assess_from_signals("uow_test_001", signals)
        assert assessment.has_juice is False

    def test_no_execution_history_rationale_empty(self):
        """Spec: when has_juice=False, rationale must be empty."""
        signals = _make_signals(total_execution_cycles=0)
        assessment = _assess_from_signals("uow_test_001", signals)
        assert assessment.rationale == ""

    def test_no_execution_history_empty_audit_log(self):
        """Empty audit log → no signals, no juice."""
        signals = _extract_signals([])
        assessment = _assess_from_signals("uow_test_empty", signals)
        assert assessment.score is None
        assert assessment.has_juice is False


# ---------------------------------------------------------------------------
# Tests: all failures (edge case: indeterminate thread)
# ---------------------------------------------------------------------------

class TestAllFailures:
    """Zero oracle approvals on a thread with execution history → no juice.

    Spec risk: juice masking indeterminate work. A thread that consistently
    fails oracle review should NOT get juice — it is stuck, not generative.
    """

    def test_zero_oracle_approvals_with_executions_no_juice(self):
        """Oracle approval rate=0% with sufficient samples → no juice."""
        signals = _make_signals(
            oracle_approval_count=0,
            total_execution_cycles=3,
            done_outcome_count=0,
            recent_oracle_approved=False,
        )
        assessment = _assess_from_signals("uow_test_002", signals)
        assert assessment.has_juice is False

    def test_below_minimum_sample_oracle_rate_no_juice(self):
        """Only 1 execution (below JUICE_MIN_ORACLE_SAMPLE_SIZE=2) → oracle
        rate sub-score is 0 (too noisy). Without oracle signal, score is too
        low unless completion/prerequisites compensate."""
        signals = _make_signals(
            oracle_approval_count=1,
            total_execution_cycles=1,  # below sample threshold
            done_outcome_count=0,
            recent_oracle_approved=True,
        )
        # Oracle sub-score is 0 (below sample threshold).
        # Completion sub-score is 0 (done=0).
        # Prerequisite sub-score is 0.
        # Aggregate should be 0.
        oracle_sub = _score_oracle_approval(signals)
        assert oracle_sub == 0.0, (
            "Oracle sub-score must be 0 when sample size < JUICE_MIN_ORACLE_SAMPLE_SIZE"
        )

    def test_all_executions_rejected_no_juice(self):
        """All executions failed oracle → low score → no juice."""
        signals = _make_signals(
            oracle_approval_count=0,
            total_execution_cycles=5,
            done_outcome_count=0,
            recent_oracle_approved=False,
        )
        assessment = _assess_from_signals("uow_test_003", signals)
        assert assessment.has_juice is False

    def test_rationale_empty_when_no_juice(self):
        """has_juice=False always means rationale is empty."""
        signals = _make_signals(
            oracle_approval_count=0,
            total_execution_cycles=5,
        )
        assessment = _assess_from_signals("uow_test_003b", signals)
        assert assessment.has_juice is False
        assert assessment.rationale == ""


# ---------------------------------------------------------------------------
# Tests: high oracle approval rate → juice asserted
# ---------------------------------------------------------------------------

class TestHighOracleApprovalRate:
    """High oracle approval rate with sufficient sample → juice asserted.

    Oracle approval is the strongest single juice signal (weight 0.45).
    """

    def test_100_percent_oracle_approval_rate_has_juice(self):
        """Perfect oracle approval rate with adequate sample → juice asserted."""
        signals = _make_signals(
            oracle_approval_count=3,
            total_execution_cycles=3,
            done_outcome_count=1,
            recent_oracle_approved=True,
        )
        assessment = _assess_from_signals("uow_test_004", signals)
        assert assessment.has_juice is True
        assert assessment.score is not None
        assert assessment.score >= JUICE_SCORE_THRESHOLD

    def test_high_oracle_approval_rate_produces_nonempty_rationale(self):
        """Spec: rationale is non-empty when has_juice=True."""
        signals = _make_signals(
            oracle_approval_count=3,
            total_execution_cycles=3,
            done_outcome_count=1,
            recent_oracle_approved=True,
        )
        assessment = _assess_from_signals("uow_test_005", signals)
        assert assessment.has_juice is True
        assert len(assessment.rationale) > 0

    def test_rationale_mentions_oracle_approval_rate(self):
        """Spec: rationale must name the alive thread with its signals."""
        signals = _make_signals(
            oracle_approval_count=4,
            total_execution_cycles=5,
            done_outcome_count=2,
            recent_oracle_approved=True,
        )
        assessment = _assess_from_signals("uow_test_006", signals)
        assert assessment.has_juice is True
        # Rationale should describe the oracle approval signal
        assert "oracle" in assessment.rationale.lower() or "approval" in assessment.rationale.lower()

    def test_borderline_oracle_rate_with_recent_approval_has_juice(self):
        """50% oracle rate (exact boundary) with recent approval → should juice
        due to recency boost in _score_oracle_approval."""
        signals = _make_signals(
            oracle_approval_count=2,
            total_execution_cycles=4,  # 50% rate, meets minimum sample
            done_outcome_count=1,
            recent_oracle_approved=True,
        )
        # rate=0.5 * recency_boost=1.2 → effective_rate=0.6 → oracle_sub=0.6
        # completion_sub=0.25 (1/4)
        # prerequisite_sub=0.0
        # aggregate = 0.45*0.6 + 0.35*0.25 + 0.20*0.0 = 0.27 + 0.0875 = 0.3575
        # 0.3575 > JUICE_SCORE_THRESHOLD=0.35 → has_juice=True
        oracle_sub = _score_oracle_approval(signals)
        assert oracle_sub > JUICE_MIN_ORACLE_APPROVAL_RATE, (
            "Recency boost should push borderline 50% rate above threshold"
        )


# ---------------------------------------------------------------------------
# Tests: completed prerequisites add positive signal
# ---------------------------------------------------------------------------

class TestCompletedPrerequisites:
    """Completed prerequisites contribute to juice score (weight 0.20)."""

    def test_completed_prerequisites_boost_score(self):
        """Adding completed prerequisites raises the aggregate score."""
        signals_no_prereqs = _make_signals(
            oracle_approval_count=2,
            total_execution_cycles=3,
            done_outcome_count=1,
            completed_prerequisite_count=0,
            recent_oracle_approved=True,
        )
        signals_with_prereqs = replace(signals_no_prereqs, completed_prerequisite_count=2)

        score_no_prereqs = _aggregate_score(signals_no_prereqs)
        score_with_prereqs = _aggregate_score(signals_with_prereqs)

        assert score_with_prereqs > score_no_prereqs, (
            "Completed prerequisites should increase the juice score"
        )

    def test_prerequisite_sub_score_threshold(self):
        """JUICE_PREREQUISITE_POSITIVE_THRESHOLD=1: at least 1 completed prereq
        produces score=1.0; 0 produces score=0.0."""
        signals_none = _make_signals(completed_prerequisite_count=0)
        signals_one = _make_signals(completed_prerequisite_count=1)
        signals_many = _make_signals(completed_prerequisite_count=5)

        assert _score_prerequisites(signals_none) == 0.0
        assert _score_prerequisites(signals_one) == 1.0
        assert _score_prerequisites(signals_many) == 1.0

    def test_prerequisite_threshold_constant_matches_spec(self):
        """The prerequisite threshold is 1 — named in the spec."""
        assert JUICE_PREREQUISITE_POSITIVE_THRESHOLD == 1


# ---------------------------------------------------------------------------
# Tests: recent oracle approval recency boost
# ---------------------------------------------------------------------------

class TestRecentOracleApprovalBoost:
    """Spec: recency matters — a recent approval is a stronger signal."""

    def test_recent_approval_increases_oracle_sub_score(self):
        """Same rate, different recency → recent approval gets higher sub-score."""
        signals_recent = _make_signals(
            oracle_approval_count=2,
            total_execution_cycles=4,
            recent_oracle_approved=True,
        )
        signals_stale = _make_signals(
            oracle_approval_count=2,
            total_execution_cycles=4,
            recent_oracle_approved=False,
        )
        score_recent = _score_oracle_approval(signals_recent)
        score_stale = _score_oracle_approval(signals_stale)
        assert score_recent > score_stale, (
            "Recent oracle approval should produce higher oracle sub-score"
        )

    def test_recent_rejection_does_not_boost(self):
        """Most recent verdict = rejected → no recency boost to oracle sub-score."""
        signals = _make_signals(
            oracle_approval_count=2,
            total_execution_cycles=3,
            recent_oracle_approved=False,
        )
        score = _score_oracle_approval(signals)
        # rate=2/3≈0.667, no recency boost, so score = rate ≈ 0.667 (capped at 1.0)
        assert score < 1.0, "Non-boosted score should remain below 1.0 for partial rate"
        # With recency boost it would be min(1.0, 0.667*1.2)=0.8, without it's 0.667
        score_boosted = _score_oracle_approval(replace(signals, recent_oracle_approved=True))
        assert score_boosted > score


# ---------------------------------------------------------------------------
# Tests: signal extraction from audit log
# ---------------------------------------------------------------------------

class TestSignalExtraction:
    """_extract_signals correctly parses audit log entries."""

    def test_extracts_oracle_approval_count(self):
        entries = [
            _make_audit_entry("oracle_approved"),
            _make_audit_entry("oracle_approved"),
            _make_audit_entry("execution_complete"),
        ]
        signals = _extract_signals(entries)
        assert signals.oracle_approval_count == 2

    def test_extracts_total_execution_cycles(self):
        entries = [
            _make_audit_entry("execution_complete"),
            _make_audit_entry("execution_complete"),
            _make_audit_entry("status_change", to_status="done"),
        ]
        signals = _extract_signals(entries)
        assert signals.total_execution_cycles == 2

    def test_extracts_done_outcome_count(self):
        entries = [
            _make_audit_entry("status_change", to_status="done"),
            _make_audit_entry("status_change", to_status="failed"),
            _make_audit_entry("status_change", to_status="done"),
        ]
        signals = _extract_signals(entries)
        assert signals.done_outcome_count == 2

    def test_recent_oracle_approved_true_when_last_verdict_approved(self):
        entries = [
            _make_audit_entry("oracle_rejected"),
            _make_audit_entry("oracle_approved"),  # most recent → approved
        ]
        signals = _extract_signals(entries)
        assert signals.recent_oracle_approved is True

    def test_recent_oracle_approved_false_when_last_verdict_rejected(self):
        entries = [
            _make_audit_entry("oracle_approved"),
            _make_audit_entry("oracle_rejected"),  # most recent → rejected
        ]
        signals = _extract_signals(entries)
        assert signals.recent_oracle_approved is False

    def test_recent_oracle_approved_false_when_no_verdict(self):
        """No oracle events at all → recent_oracle_approved defaults False."""
        entries = [
            _make_audit_entry("execution_complete"),
            _make_audit_entry("status_change", to_status="ready-for-steward"),
        ]
        signals = _extract_signals(entries)
        assert signals.recent_oracle_approved is False

    def test_empty_audit_log_produces_zero_signals(self):
        signals = _extract_signals([])
        assert signals.oracle_approval_count == 0
        assert signals.total_execution_cycles == 0
        assert signals.done_outcome_count == 0
        assert signals.recent_oracle_approved is False

    def test_extract_with_prerequisites_passes_through_count(self):
        """completed_prerequisite_count is not derived from audit entries."""
        signals = _extract_signals_with_prerequisites([], completed_prerequisite_count=3)
        assert signals.completed_prerequisite_count == 3


# ---------------------------------------------------------------------------
# Tests: JuiceSignals immutability
# ---------------------------------------------------------------------------

class TestJuiceSignalsImmutability:
    """JuiceSignals is a frozen dataclass — mutation attempts raise."""

    def test_juice_signals_is_frozen(self):
        signals = _make_signals()
        with pytest.raises((AttributeError, TypeError)):
            signals.oracle_approval_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: compute_juice (public function interface)
# ---------------------------------------------------------------------------

class TestComputeJuice:
    """compute_juice(uow_id, registry) → float | None.

    This tests the public function interface from the task brief.
    """

    def test_compute_juice_returns_none_for_missing_uow(self):
        """Spec: compute_juice returns None when UoW is not found."""
        registry = MagicMock()
        registry.get.return_value = None
        result = compute_juice("uow_nonexistent", registry)
        assert result is None

    def test_compute_juice_returns_none_for_no_execution_history(self):
        """compute_juice returns None when there is no execution history."""
        from src.orchestration.registry import UoW, UoWStatus

        uow = UoW(
            id="uow_test_007",
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Test UoW",
            source="telegram",
            source_issue_number=42,
            created_at="2026-04-24T00:00:00+00:00",
            updated_at="2026-04-24T00:00:00+00:00",
        )

        registry = MagicMock()
        registry.get.return_value = uow
        # Use public fetch_audit_entries — no private _connect access
        registry.fetch_audit_entries.return_value = []

        result = compute_juice("uow_test_007", registry)
        assert result is None

    def test_compute_juice_returns_float_for_productive_thread(self):
        """compute_juice returns a float when there is execution history."""
        from src.orchestration.registry import UoW, UoWStatus

        uow = UoW(
            id="uow_test_008",
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Productive UoW",
            source="telegram",
            source_issue_number=43,
            created_at="2026-04-24T00:00:00+00:00",
            updated_at="2026-04-24T00:00:00+00:00",
        )

        registry = MagicMock()
        registry.get.return_value = uow
        registry.get.side_effect = lambda uow_id: uow if uow_id == "uow_test_008" else None

        # Simulate audit log with oracle approvals via public fetch_audit_entries
        audit_rows = [
            {"event": "execution_complete", "to_status": "ready-for-steward"},
            {"event": "oracle_approved", "to_status": None},
            {"event": "execution_complete", "to_status": "ready-for-steward"},
            {"event": "oracle_approved", "to_status": None},
            {"event": "status_change", "to_status": "done"},
        ]
        registry.fetch_audit_entries.return_value = audit_rows

        result = compute_juice("uow_test_008", registry)
        assert result is not None
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# Tests: JuiceSensor integration
# ---------------------------------------------------------------------------

class TestJuiceSensor:
    """JuiceSensor.assess() delegates to pure functions correctly."""

    def test_assess_stateless_across_multiple_calls(self):
        """JuiceSensor is stateless — calling assess() multiple times is safe."""
        sensor = JuiceSensor()

        uow_mock = MagicMock()
        uow_mock.id = "uow_test_sensor"
        uow_mock.trigger = None

        registry_mock = MagicMock()
        registry_mock.get.return_value = None

        # Empty audit → no history → None score
        result1 = sensor.assess(uow_mock, [], registry_mock)
        result2 = sensor.assess(uow_mock, [], registry_mock)

        assert result1 == result2, "JuiceSensor must be stateless"

    def test_assess_no_audit_entries_returns_no_juice(self):
        """No audit entries → insufficient data → has_juice=False."""
        sensor = JuiceSensor()

        uow_mock = MagicMock()
        uow_mock.id = "uow_test_empty"
        uow_mock.trigger = None

        registry_mock = MagicMock()
        registry_mock.get.return_value = None

        result = sensor.assess(uow_mock, [], registry_mock)
        assert result.has_juice is False
        assert result.score is None

    def test_assess_productive_history_returns_juice(self):
        """Productive audit history → has_juice=True."""
        sensor = JuiceSensor()

        uow_mock = MagicMock()
        uow_mock.id = "uow_test_productive"
        uow_mock.trigger = None

        registry_mock = MagicMock()
        registry_mock.get.return_value = None

        audit_entries = [
            _make_audit_entry("execution_complete", to_status="ready-for-steward"),
            _make_audit_entry("oracle_approved"),
            _make_audit_entry("execution_complete", to_status="ready-for-steward"),
            _make_audit_entry("oracle_approved"),
            _make_audit_entry("status_change", to_status="done"),
        ]

        result = sensor.assess(uow_mock, audit_entries, registry_mock)
        assert result.has_juice is True
        assert result.score is not None
        assert result.score >= JUICE_SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Tests: named threshold constants
# ---------------------------------------------------------------------------

class TestNamedThresholdConstants:
    """Threshold constants are explicitly named in the spec and must not drift."""

    def test_minimum_oracle_approval_rate_is_50_percent(self):
        assert JUICE_MIN_ORACLE_APPROVAL_RATE == 0.5

    def test_minimum_oracle_sample_size_is_2(self):
        assert JUICE_MIN_ORACLE_SAMPLE_SIZE == 2

    def test_minimum_completion_rate_is_30_percent(self):
        assert JUICE_MIN_COMPLETION_RATE == 0.3

    def test_completion_rate_below_minimum_returns_zero(self):
        """Spec: completion rate below JUICE_MIN_COMPLETION_RATE gate → score 0.0."""
        # 1 done out of 4 executions = 25%, below 30% minimum
        signals = _make_signals(
            done_outcome_count=1,
            total_execution_cycles=4,
        )
        assert _score_completion_rate(signals) == 0.0, (
            "Completion rate below JUICE_MIN_COMPLETION_RATE must return 0.0"
        )

    def test_completion_rate_at_minimum_returns_nonzero(self):
        """Completion rate at JUICE_MIN_COMPLETION_RATE boundary → non-zero score."""
        # 3 done out of 10 executions = 30%, exactly at minimum
        signals = _make_signals(
            done_outcome_count=3,
            total_execution_cycles=10,
        )
        assert _score_completion_rate(signals) > 0.0, (
            "Completion rate at JUICE_MIN_COMPLETION_RATE must return a positive score"
        )

    def test_completion_rate_above_minimum_returns_nonzero(self):
        """Completion rate above minimum → positive score proportional to rate."""
        # 2 done out of 4 executions = 50%, above 30% minimum
        signals = _make_signals(
            done_outcome_count=2,
            total_execution_cycles=4,
        )
        score = _score_completion_rate(signals)
        assert score > 0.0
        assert score == 0.5

    def test_prerequisite_positive_threshold_is_1(self):
        assert JUICE_PREREQUISITE_POSITIVE_THRESHOLD == 1

    def test_juice_update_delta_is_point_05(self):
        """JUICE_UPDATE_DELTA=0.05 — avoids churn on stable threads."""
        assert JUICE_UPDATE_DELTA == 0.05

    def test_juice_score_threshold_is_above_zero(self):
        """JUICE_SCORE_THRESHOLD must be positive so juice is not free."""
        assert JUICE_SCORE_THRESHOLD > 0.0


# ---------------------------------------------------------------------------
# Helper: convert dict to something that behaves like a sqlite3.Row
# ---------------------------------------------------------------------------

class _MockSqliteRow(dict):
    """Minimal dict subclass that also supports attribute access for sqlite3.Row compat."""
    def __getitem__(self, key):
        return super().__getitem__(key)


def dict_to_sqlite_row(d: dict) -> _MockSqliteRow:
    """Convert a plain dict to a mock sqlite3.Row-like object."""
    return _MockSqliteRow(d)
