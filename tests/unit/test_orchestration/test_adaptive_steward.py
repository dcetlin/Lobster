"""
Tests for WOS Evolution Stage 1: Adaptive Steward.

Covers:
- Migration 0029 idempotency: verdict_accumulator, prescription_hypothesis_log,
  normalization_log tables exist after migration.
- PrescriptionObject construction: hypothesis truncation, frozen immutability.
- hypothesis_from_uow_summary: truncation at 140 chars.
- normalize_hypothesis_text: Haiku call (mocked), returns truncated string.
- score_and_normalize_verdict: end-to-end path (mocked Haiku), writes to
  normalization_log and verdict_accumulator.
- Verdict upsert idempotency: duplicate calls increment, don't reset.
- Rollup queries: get_hypothesis_rollup, accumulator_total_scored.
- Gate condition: GATE_SCORED_OUTCOMES_MIN constant and gate_ready flag.
- get_top_priors: returns hypotheses ordered by success_rate DESC.
- AccumulatorSummary: pearl_count, gate_ready.
- log_prescription_hypothesis and get_hypothesis_for_uow round-trip.
- _score_verdict_for_uow fallback path (no log row → uses summary).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations, _MIGRATIONS_DIR
from orchestration.prescription_object import (
    HYPOTHESIS_MAX_CHARS,
    PrescriptionObject,
    build_prescription_object,
    hypothesis_from_uow_summary,
)
from orchestration.registry import UoWRegister
from orchestration.verdict_normalization import (
    HAIKU_MODEL,
    _ensure_schema,
    _log_normalization,
    _mark_hypothesis_scored,
    _upsert_verdict,
    get_hypothesis_for_uow,
    log_prescription_hypothesis,
    normalize_hypothesis_text,
    score_and_normalize_verdict,
)
from orchestration.verdict_stats import (
    GATE_SCORED_OUTCOMES_MIN,
    PEARL_MIN_OBSERVATIONS,
    PEARL_SUCCESS_RATE_THRESHOLD,
    AccumulatorSummary,
    get_accumulator_summary,
    get_hypothesis_rollup,
    get_normalization_cluster_health,
    get_top_priors,
    accumulator_total_scored,
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(db_path: Path) -> set[str]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration 0029 idempotency
# ---------------------------------------------------------------------------

class TestMigration0029:
    """Migration 0029 creates the three new tables idempotently."""

    def test_verdict_accumulator_table_created(self, tmp_path: Path) -> None:
        """verdict_accumulator table exists after running migrations."""
        db_path = tmp_path / "wos.db"
        run_migrations(db_path)
        assert "verdict_accumulator" in _table_names(db_path)

    def test_prescription_hypothesis_log_table_created(self, tmp_path: Path) -> None:
        """prescription_hypothesis_log table exists after running migrations."""
        db_path = tmp_path / "wos.db"
        run_migrations(db_path)
        assert "prescription_hypothesis_log" in _table_names(db_path)

    def test_normalization_log_table_created(self, tmp_path: Path) -> None:
        """normalization_log table exists after running migrations."""
        db_path = tmp_path / "wos.db"
        run_migrations(db_path)
        assert "normalization_log" in _table_names(db_path)

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Running migrations twice is safe (idempotent CREATE TABLE IF NOT EXISTS)."""
        db_path = tmp_path / "wos.db"
        first = run_migrations(db_path)
        second = run_migrations(db_path)
        assert 29 in first
        assert 29 not in second  # already applied

    def test_verdict_accumulator_unique_constraint(self, tmp_path: Path) -> None:
        """verdict_accumulator enforces UNIQUE(register, diagnosis_hypothesis)."""
        db_path = tmp_path / "wos.db"
        run_migrations(db_path)
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO verdict_accumulator "
                "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
                "VALUES ('operational', 'test hypothesis', 1, 0, 0, '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO verdict_accumulator "
                    "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
                    "VALUES ('operational', 'test hypothesis', 0, 1, 0, '2026-01-01T00:00:00Z')"
                )
                conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PrescriptionObject
# ---------------------------------------------------------------------------

class TestPrescriptionObject:
    """PrescriptionObject construction and validation."""

    def test_construction_with_valid_fields(self) -> None:
        """PrescriptionObject can be constructed with all required fields."""
        obj = PrescriptionObject(
            uow_id="uow_test_001",
            register=UoWRegister.OPERATIONAL,
            diagnosis_hypothesis="Missing retry logic causes intermittent 503s",
            proposed_steps=["Add retry with exponential backoff"],
            confidence=0.8,
            counterfactual_question="Would the issue persist with retries?",
            generated_at="2026-01-01T00:00:00Z",
            selector_priors=[],
        )
        assert obj.uow_id == "uow_test_001"
        assert obj.confidence == 0.8

    def test_hypothesis_max_chars_enforced(self) -> None:
        """Hypothesis exceeding 140 chars raises ValueError at construction."""
        with pytest.raises(ValueError, match="exceeds.*chars"):
            PrescriptionObject(
                uow_id="uow_test_001",
                register=UoWRegister.OPERATIONAL,
                diagnosis_hypothesis="x" * (HYPOTHESIS_MAX_CHARS + 1),
                proposed_steps=[],
                confidence=0.5,
                counterfactual_question="",
                generated_at="2026-01-01T00:00:00Z",
                selector_priors=[],
            )

    def test_hypothesis_exactly_max_chars_is_valid(self) -> None:
        """Hypothesis at exactly 140 chars is accepted."""
        obj = PrescriptionObject(
            uow_id="uow_test_001",
            register=UoWRegister.OPERATIONAL,
            diagnosis_hypothesis="x" * HYPOTHESIS_MAX_CHARS,
            proposed_steps=[],
            confidence=0.5,
            counterfactual_question="",
            generated_at="2026-01-01T00:00:00Z",
            selector_priors=[],
        )
        assert len(obj.diagnosis_hypothesis) == HYPOTHESIS_MAX_CHARS

    def test_frozen_immutability(self) -> None:
        """PrescriptionObject instances are immutable (frozen dataclass)."""
        obj = PrescriptionObject(
            uow_id="uow_test_001",
            register=UoWRegister.OPERATIONAL,
            diagnosis_hypothesis="Short hypothesis",
            proposed_steps=[],
            confidence=0.5,
            counterfactual_question="",
            generated_at="2026-01-01T00:00:00Z",
            selector_priors=[],
        )
        with pytest.raises((AttributeError, TypeError)):
            obj.uow_id = "something_else"  # type: ignore[misc]

    def test_build_prescription_object_truncates_hypothesis(self) -> None:
        """build_prescription_object truncates over-long hypotheses."""
        long_hypothesis = "a" * 200
        obj = build_prescription_object(
            uow_id="uow_test_001",
            register=UoWRegister.OPERATIONAL,
            hypothesis_raw=long_hypothesis,
            proposed_steps=[],
            confidence=0.5,
            counterfactual_question="",
            generated_at="2026-01-01T00:00:00Z",
        )
        assert len(obj.diagnosis_hypothesis) == HYPOTHESIS_MAX_CHARS
        assert obj.diagnosis_hypothesis == "a" * HYPOTHESIS_MAX_CHARS


class TestHypothesisFromSummary:
    """hypothesis_from_uow_summary derivation."""

    def test_truncates_to_max_chars(self) -> None:
        """Long summary is truncated to HYPOTHESIS_MAX_CHARS."""
        long_summary = "b" * 300
        result = hypothesis_from_uow_summary(long_summary)
        assert len(result) == HYPOTHESIS_MAX_CHARS

    def test_short_summary_unchanged(self) -> None:
        """Short summary is returned unchanged."""
        short = "Fix the login bug"
        assert hypothesis_from_uow_summary(short) == short

    def test_empty_summary_returns_empty(self) -> None:
        """Empty summary returns empty string."""
        assert hypothesis_from_uow_summary("") == ""

    def test_strips_leading_trailing_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped."""
        padded = "  Fix the login bug  "
        assert hypothesis_from_uow_summary(padded) == "Fix the login bug"


# ---------------------------------------------------------------------------
# normalize_hypothesis_text (mocked Haiku call)
# ---------------------------------------------------------------------------

class TestNormalizeHypothesisText:
    """normalize_hypothesis_text calls Haiku and returns a normalized string."""

    def _make_mock_client(self, normalized_text: str) -> MagicMock:
        """Build a mock anthropic.Anthropic client returning *normalized_text*."""
        content_block = MagicMock()
        content_block.text = normalized_text

        response = MagicMock()
        response.content = [content_block]

        client = MagicMock()
        client.messages.create.return_value = response
        return client

    def test_returns_normalized_string(self) -> None:
        """normalize_hypothesis_text returns the Haiku-normalized form."""
        mock_client = self._make_mock_client("Missing retry logic causes 503s; add exponential backoff.")
        result = normalize_hypothesis_text("missing retry causes 503", anthropic_client=mock_client)
        assert result == "Missing retry logic causes 503s; add exponential backoff."

    def test_truncates_to_max_chars(self) -> None:
        """If Haiku returns >140 chars, result is truncated."""
        mock_client = self._make_mock_client("x" * 200)
        result = normalize_hypothesis_text("some hypothesis", anthropic_client=mock_client)
        assert len(result) == HYPOTHESIS_MAX_CHARS

    def test_calls_haiku_model(self) -> None:
        """normalize_hypothesis_text uses the HAIKU_MODEL by default."""
        mock_client = self._make_mock_client("normalized")
        normalize_hypothesis_text("some hypothesis", anthropic_client=mock_client)
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == HAIKU_MODEL

    def test_includes_cache_control_on_system_prompt(self) -> None:
        """System prompt is sent with cache_control for prompt caching."""
        mock_client = self._make_mock_client("normalized")
        normalize_hypothesis_text("some hypothesis", anthropic_client=mock_client)
        call_kwargs = mock_client.messages.create.call_args[1]
        system = call_kwargs["system"]
        assert isinstance(system, list)
        assert any("cache_control" in block for block in system)

    def test_raises_on_empty_response(self) -> None:
        """RuntimeError raised when Haiku returns empty content."""
        response = MagicMock()
        response.content = []
        client = MagicMock()
        client.messages.create.return_value = response
        with pytest.raises(RuntimeError):
            normalize_hypothesis_text("some hypothesis", anthropic_client=client)


# ---------------------------------------------------------------------------
# score_and_normalize_verdict (mocked Haiku, tmp DB)
# ---------------------------------------------------------------------------

class TestScoreAndNormalizeVerdict:
    """score_and_normalize_verdict writes to normalization_log and verdict_accumulator."""

    def _make_mock_client(self, normalized: str = "Normalized hypothesis") -> MagicMock:
        content_block = MagicMock()
        content_block.text = normalized
        response = MagicMock()
        response.content = [content_block]
        client = MagicMock()
        client.messages.create.return_value = response
        return client

    def test_writes_to_normalization_log(self, tmp_path: Path) -> None:
        """After scoring, normalization_log contains one row for the UoW."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("Normalized form")

        score_and_normalize_verdict(
            uow_id="uow_test_001",
            register="operational",
            hypothesis_raw="raw hypothesis string",
            outcome="pass",
            anthropic_client=mock_client,
            db_path=db_path,
        )

        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM normalization_log").fetchall()
            assert len(rows) == 1
            assert rows[0]["uow_id"] == "uow_test_001"
            assert rows[0]["raw"] == "raw hypothesis string"
            assert rows[0]["normalized"] == "Normalized form"
        finally:
            conn.close()

    def test_writes_to_verdict_accumulator(self, tmp_path: Path) -> None:
        """After scoring, verdict_accumulator contains one row with n_successes=1."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("Canonical hypothesis")

        score_and_normalize_verdict(
            uow_id="uow_test_001",
            register="operational",
            hypothesis_raw="raw text",
            outcome="pass",
            anthropic_client=mock_client,
            db_path=db_path,
        )

        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM verdict_accumulator").fetchall()
            assert len(rows) == 1
            assert rows[0]["n_successes"] == 1
            assert rows[0]["n_failures"] == 0
        finally:
            conn.close()

    def test_fail_outcome_increments_n_failures(self, tmp_path: Path) -> None:
        """outcome='fail' increments n_failures in verdict_accumulator."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("Same hypothesis")

        score_and_normalize_verdict(
            uow_id="uow_test_001",
            register="operational",
            hypothesis_raw="raw text",
            outcome="fail",
            anthropic_client=mock_client,
            db_path=db_path,
        )

        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM verdict_accumulator").fetchall()
            assert rows[0]["n_failures"] == 1
            assert rows[0]["n_successes"] == 0
        finally:
            conn.close()

    def test_partial_outcome_increments_n_partial(self, tmp_path: Path) -> None:
        """outcome='partial' increments n_partial in verdict_accumulator."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("Same hypothesis")

        score_and_normalize_verdict(
            uow_id="uow_test_002",
            register="iterative-convergent",
            hypothesis_raw="raw text",
            outcome="partial",
            anthropic_client=mock_client,
            db_path=db_path,
        )

        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM verdict_accumulator").fetchall()
            assert rows[0]["n_partial"] == 1
        finally:
            conn.close()

    def test_duplicate_calls_increment_not_reset(self, tmp_path: Path) -> None:
        """Two 'pass' verdicts for the same hypothesis yield n_successes=2."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("Same hypothesis")

        for i in range(2):
            score_and_normalize_verdict(
                uow_id=f"uow_test_{i:03d}",
                register="operational",
                hypothesis_raw="raw text",
                outcome="pass",
                anthropic_client=mock_client,
                db_path=db_path,
            )

        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM verdict_accumulator").fetchall()
            assert len(rows) == 1  # same normalized hypothesis → same row
            assert rows[0]["n_successes"] == 2
        finally:
            conn.close()

    def test_empty_hypothesis_is_skipped(self, tmp_path: Path) -> None:
        """Empty hypothesis string does not write to the DB."""
        db_path = tmp_path / "wos-metrics.db"
        mock_client = self._make_mock_client("anything")

        score_and_normalize_verdict(
            uow_id="uow_test_001",
            register="operational",
            hypothesis_raw="",
            outcome="pass",
            anthropic_client=mock_client,
            db_path=db_path,
        )

        # DB should not have been created or have no rows
        if db_path.exists():
            conn = _connect(db_path)
            try:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                table_names = {r["name"] for r in tables}
                if "normalization_log" in table_names:
                    rows = conn.execute("SELECT * FROM normalization_log").fetchall()
                    assert len(rows) == 0
            finally:
                conn.close()

    def test_haiku_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """If Haiku fails, score_and_normalize_verdict does not raise."""
        db_path = tmp_path / "wos-metrics.db"
        failing_client = MagicMock()
        failing_client.messages.create.side_effect = RuntimeError("API error")

        # Must not raise
        score_and_normalize_verdict(
            uow_id="uow_test_001",
            register="operational",
            hypothesis_raw="some hypothesis",
            outcome="pass",
            anthropic_client=failing_client,
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# log_prescription_hypothesis + get_hypothesis_for_uow round-trip
# ---------------------------------------------------------------------------

class TestPrescriptionHypothesisLogRoundTrip:
    """log_prescription_hypothesis and get_hypothesis_for_uow work together."""

    def test_round_trip_returns_stored_hypothesis(self, tmp_path: Path) -> None:
        """Hypothesis written by log_prescription_hypothesis is retrievable."""
        db_path = tmp_path / "wos-metrics.db"

        log_prescription_hypothesis(
            uow_id="uow_test_001",
            hypothesis="Missing index causes slow queries on accounts table",
            register="operational",
            generated_at="2026-01-01T00:00:00Z",
            db_path=db_path,
        )

        result = get_hypothesis_for_uow("uow_test_001", db_path=db_path)
        assert result == "Missing index causes slow queries on accounts table"

    def test_returns_none_for_unknown_uow(self, tmp_path: Path) -> None:
        """get_hypothesis_for_uow returns None when no row exists."""
        db_path = tmp_path / "wos-metrics.db"
        result = get_hypothesis_for_uow("uow_does_not_exist", db_path=db_path)
        assert result is None

    def test_returns_most_recent_unscored_row(self, tmp_path: Path) -> None:
        """When multiple rows exist, the most recent unscored is returned."""
        db_path = tmp_path / "wos-metrics.db"

        log_prescription_hypothesis(
            uow_id="uow_test_001",
            hypothesis="Old hypothesis",
            register="operational",
            generated_at="2026-01-01T00:00:00Z",
            db_path=db_path,
        )
        log_prescription_hypothesis(
            uow_id="uow_test_001",
            hypothesis="New hypothesis",
            register="operational",
            generated_at="2026-01-02T00:00:00Z",
            db_path=db_path,
        )

        result = get_hypothesis_for_uow("uow_test_001", db_path=db_path)
        assert result == "New hypothesis"

    def test_scored_rows_are_excluded(self, tmp_path: Path) -> None:
        """After scoring, get_hypothesis_for_uow returns None."""
        db_path = tmp_path / "wos-metrics.db"

        # Ensure schema
        conn = sqlite3.connect(str(db_path))
        _ensure_schema(conn)
        conn.commit()

        log_prescription_hypothesis(
            uow_id="uow_test_001",
            hypothesis="Some hypothesis",
            register="operational",
            generated_at="2026-01-01T00:00:00Z",
            db_path=db_path,
        )

        # Mark as scored
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        _mark_hypothesis_scored(conn2, "uow_test_001", "pass")
        conn2.commit()
        conn2.close()

        result = get_hypothesis_for_uow("uow_test_001", db_path=db_path)
        assert result is None


# ---------------------------------------------------------------------------
# Rollup queries
# ---------------------------------------------------------------------------

class TestRollupQueries:
    """get_hypothesis_rollup, accumulator_total_scored, get_accumulator_summary."""

    def _seed_db(self, db_path: Path) -> None:
        """Seed the DB with test data for rollup queries."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        rows = [
            ("operational", "Hypothesis A: missing retry logic", 7, 1, 0, "2026-01-01T00:00:00Z"),
            ("operational", "Hypothesis B: stale cache causing reads", 3, 3, 2, "2026-01-02T00:00:00Z"),
            ("iterative-convergent", "Hypothesis C: convergence threshold too tight", 2, 0, 1, "2026-01-03T00:00:00Z"),
        ]
        for register, hyp, n_s, n_f, n_p, ts in rows:
            conn.execute(
                "INSERT INTO verdict_accumulator "
                "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (register, hyp, n_s, n_f, n_p, ts),
            )
        conn.commit()
        conn.close()

    def test_rollup_returns_all_rows(self, tmp_path: Path) -> None:
        """get_hypothesis_rollup with no register filter returns all rows."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)
        results = get_hypothesis_rollup(db_path=db_path)
        assert len(results) == 3

    def test_rollup_filtered_by_register(self, tmp_path: Path) -> None:
        """get_hypothesis_rollup filtered by register returns only that register."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)
        results = get_hypothesis_rollup(register="operational", db_path=db_path)
        assert len(results) == 2
        assert all(r.register == "operational" for r in results)

    def test_rollup_ordered_by_success_rate_desc(self, tmp_path: Path) -> None:
        """Results are ordered by success_rate descending."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)
        results = get_hypothesis_rollup(register="operational", db_path=db_path)
        rates = [r.success_rate for r in results]
        assert rates == sorted(rates, reverse=True)

    def test_rollup_computes_total_and_rate(self, tmp_path: Path) -> None:
        """total and success_rate are computed correctly."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)
        results = get_hypothesis_rollup(register="operational", db_path=db_path)
        # Find Hypothesis A: 7 successes, 1 failure, 0 partial → total=8, rate=7/8=0.875
        hyp_a = next(r for r in results if "Hypothesis A" in r.diagnosis_hypothesis)
        assert hyp_a.total == 8
        assert abs(hyp_a.success_rate - 7 / 8) < 0.001

    def test_rollup_returns_empty_on_missing_db(self, tmp_path: Path) -> None:
        """Returns empty list when DB does not exist."""
        db_path = tmp_path / "nonexistent.db"
        results = get_hypothesis_rollup(db_path=db_path)
        assert results == []

    def test_accumulator_total_scored(self, tmp_path: Path) -> None:
        """accumulator_total_scored counts rows with at least one observation."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)
        total = accumulator_total_scored(db_path=db_path)
        assert total == 3

    def test_accumulator_total_scored_zero_on_missing_db(self, tmp_path: Path) -> None:
        """Returns 0 when DB does not exist."""
        db_path = tmp_path / "nonexistent.db"
        assert accumulator_total_scored(db_path=db_path) == 0

    def test_gate_condition_not_met_below_threshold(self, tmp_path: Path) -> None:
        """gate_ready is False when total_scored < GATE_SCORED_OUTCOMES_MIN."""
        db_path = tmp_path / "wos-metrics.db"
        self._seed_db(db_path)  # seeds 3 rows, threshold is 20
        summary = get_accumulator_summary(db_path=db_path)
        assert summary.total_scored == 3
        assert summary.gate_ready is False

    def test_gate_condition_met_at_threshold(self, tmp_path: Path) -> None:
        """gate_ready is True when total_scored >= GATE_SCORED_OUTCOMES_MIN."""
        db_path = tmp_path / "wos-metrics.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        # Insert exactly GATE_SCORED_OUTCOMES_MIN rows
        for i in range(GATE_SCORED_OUTCOMES_MIN):
            conn.execute(
                "INSERT INTO verdict_accumulator "
                "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("operational", f"Hypothesis {i}", 1, 0, 0, "2026-01-01T00:00:00Z"),
            )
        conn.commit()
        conn.close()

        summary = get_accumulator_summary(db_path=db_path)
        assert summary.total_scored == GATE_SCORED_OUTCOMES_MIN
        assert summary.gate_ready is True

    def test_pearl_count_computed_correctly(self, tmp_path: Path) -> None:
        """pearl_count counts hypotheses with success_rate >= 0.7 and total >= 5."""
        db_path = tmp_path / "wos-metrics.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        # Pearl: 7 successes, 1 failure (rate=0.875 ≥ 0.7, total=8 ≥ 5)
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("operational", "Pearl hypothesis", 7, 1, 0, "2026-01-01T00:00:00Z"),
        )
        # Not a pearl: 2 successes, 8 failures (rate=0.2 < 0.7)
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("operational", "Non-pearl hypothesis", 2, 8, 0, "2026-01-01T00:00:00Z"),
        )
        # Not a pearl: 5 successes but total=5 (≥ 5) but rate=5/5=1.0 — this IS a pearl
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("operational", "Another pearl", 5, 0, 0, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        summary = get_accumulator_summary(db_path=db_path)
        assert summary.pearl_count == 2  # "Pearl hypothesis" and "Another pearl"


# ---------------------------------------------------------------------------
# get_top_priors (Selector query)
# ---------------------------------------------------------------------------

class TestGetTopPriors:
    """get_top_priors returns top-5 hypotheses by success_rate."""

    def test_returns_hypotheses_ordered_by_success_rate(self, tmp_path: Path) -> None:
        """get_top_priors returns hypotheses in success_rate descending order."""
        db_path = tmp_path / "wos-metrics.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES ('operational', 'Hypothesis A', 8, 2, 0, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES ('operational', 'Hypothesis B', 3, 7, 0, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES ('operational', 'Hypothesis C', 5, 1, 0, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        priors = get_top_priors("operational", db_path=db_path)
        # A: 8/10=0.8, C: 5/6=0.833, B: 3/10=0.3 → order: C, A, B
        assert priors[0] == "Hypothesis C"
        assert priors[1] == "Hypothesis A"
        assert priors[2] == "Hypothesis B"

    def test_respects_limit_parameter(self, tmp_path: Path) -> None:
        """get_top_priors respects the limit parameter."""
        db_path = tmp_path / "wos-metrics.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        for i in range(10):
            conn.execute(
                "INSERT INTO verdict_accumulator "
                "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
                "VALUES ('operational', ?, 1, 0, 0, '2026-01-01T00:00:00Z')",
                (f"Hypothesis {i}",),
            )
        conn.commit()
        conn.close()

        priors = get_top_priors("operational", limit=3, db_path=db_path)
        assert len(priors) == 3

    def test_returns_empty_list_when_no_data(self, tmp_path: Path) -> None:
        """Returns empty list when no data for register."""
        db_path = tmp_path / "nonexistent.db"
        priors = get_top_priors("operational", db_path=db_path)
        assert priors == []

    def test_filters_by_register(self, tmp_path: Path) -> None:
        """get_top_priors only returns hypotheses for the requested register."""
        db_path = tmp_path / "wos-metrics.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES ('operational', 'Operational hypothesis', 5, 0, 0, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO verdict_accumulator "
            "(register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated) "
            "VALUES ('iterative-convergent', 'IC hypothesis', 3, 0, 0, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        priors = get_top_priors("operational", db_path=db_path)
        assert len(priors) == 1
        assert priors[0] == "Operational hypothesis"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    """Named constants match spec requirements."""

    def test_gate_scored_outcomes_min_is_20(self) -> None:
        """GATE_SCORED_OUTCOMES_MIN must be 20 per spec §4 transition table."""
        assert GATE_SCORED_OUTCOMES_MIN == 20

    def test_hypothesis_max_chars_is_140(self) -> None:
        """HYPOTHESIS_MAX_CHARS must be 140 per spec §3-I PrescriptionObject."""
        assert HYPOTHESIS_MAX_CHARS == 140

    def test_pearl_success_rate_threshold_is_0_7(self) -> None:
        """PEARL_SUCCESS_RATE_THRESHOLD must be 0.7 per spec §5 glossary."""
        assert PEARL_SUCCESS_RATE_THRESHOLD == 0.7

    def test_pearl_min_observations_is_5(self) -> None:
        """PEARL_MIN_OBSERVATIONS must be 5 per spec §5 glossary."""
        assert PEARL_MIN_OBSERVATIONS == 5

    def test_haiku_model_id(self) -> None:
        """HAIKU_MODEL must use the claude-haiku-4-5 model."""
        assert HAIKU_MODEL == "claude-haiku-4-5"
