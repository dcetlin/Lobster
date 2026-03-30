"""
Unit tests for audit_queries.py.

Tests use an in-memory (tmp_path) registry DB populated via the Registry
class, then query through audit_queries functions. This verifies that the
query layer reads the same data the registry writes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.orchestration.registry import Registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_audit(
    db_path: Path,
    *,
    uow_id: str,
    event: str,
    ts: str,
    from_status: str | None = None,
    to_status: str | None = None,
    agent: str | None = None,
    note: str | None = None,
) -> None:
    """Insert a raw audit_log entry for test setup."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) VALUES (?,?,?,?,?,?,?)",
        (ts, uow_id, event, from_status, to_status, agent, note),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    # Initialize schema via Registry
    Registry(path)
    return path


# ---------------------------------------------------------------------------
# recent_transitions
# ---------------------------------------------------------------------------

class TestRecentTransitions:
    def test_returns_entries_for_uow_newest_first(self, db_path):
        from src.orchestration.audit_queries import recent_transitions

        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-01T10:00:00+00:00", from_status="proposed", to_status="pending")
        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-01T11:00:00+00:00", from_status="pending", to_status="active")
        _seed_audit(db_path, uow_id="uow-2", event="status_change",
                    ts="2026-01-01T12:00:00+00:00", from_status="proposed", to_status="pending")

        results = recent_transitions("uow-1", registry_path=db_path)

        assert len(results) == 2
        # Newest first — id is AUTOINCREMENT so second insert has higher id
        assert results[0]["to_status"] == "active"
        assert results[1]["to_status"] == "pending"

    def test_does_not_return_other_uow_entries(self, db_path):
        from src.orchestration.audit_queries import recent_transitions

        _seed_audit(db_path, uow_id="uow-A", event="status_change", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-B", event="status_change", ts="2026-01-01T10:00:00+00:00")

        results = recent_transitions("uow-A", registry_path=db_path)
        assert all(r["uow_id"] == "uow-A" for r in results)

    def test_limit_respected(self, db_path):
        from src.orchestration.audit_queries import recent_transitions

        for i in range(10):
            _seed_audit(db_path, uow_id="uow-1", event="status_change",
                        ts=f"2026-01-01T{i:02d}:00:00+00:00")

        results = recent_transitions("uow-1", limit=3, registry_path=db_path)
        assert len(results) == 3

    def test_empty_for_unknown_uow(self, db_path):
        from src.orchestration.audit_queries import recent_transitions

        results = recent_transitions("does-not-exist", registry_path=db_path)
        assert results == []

    def test_returns_plain_dicts(self, db_path):
        from src.orchestration.audit_queries import recent_transitions

        _seed_audit(db_path, uow_id="uow-1", event="status_change", ts="2026-01-01T10:00:00+00:00")

        results = recent_transitions("uow-1", registry_path=db_path)
        assert isinstance(results[0], dict)


# ---------------------------------------------------------------------------
# stall_events
# ---------------------------------------------------------------------------

class TestStallEvents:
    def test_returns_stall_detected_events_since(self, db_path):
        from src.orchestration.audit_queries import stall_events

        cutoff = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="stall_detected",
                    ts="2026-01-01T11:00:00+00:00")  # before cutoff
        _seed_audit(db_path, uow_id="uow-1", event="stall_detected",
                    ts="2026-01-01T13:00:00+00:00")  # after cutoff
        _seed_audit(db_path, uow_id="uow-2", event="stall_detected",
                    ts="2026-01-01T14:00:00+00:00")  # after cutoff

        results = stall_events(cutoff, registry_path=db_path)

        assert len(results) == 2
        assert all(r["event"] == "stall_detected" for r in results)
        assert results[0]["ts"] < results[1]["ts"]  # ascending order

    def test_excludes_non_stall_events(self, db_path):
        from src.orchestration.audit_queries import stall_events

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-02T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="stall_detected",
                    ts="2026-01-02T10:00:00+00:00")

        results = stall_events(cutoff, registry_path=db_path)

        assert len(results) == 1
        assert results[0]["event"] == "stall_detected"

    def test_empty_when_none_in_window(self, db_path):
        from src.orchestration.audit_queries import stall_events

        cutoff = datetime(2026, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="stall_detected",
                    ts="2026-01-01T10:00:00+00:00")

        results = stall_events(cutoff, registry_path=db_path)
        assert results == []

    def test_naive_datetime_treated_as_utc(self, db_path):
        """A naive datetime should not raise and should produce a valid filter."""
        from src.orchestration.audit_queries import stall_events

        cutoff = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
        _seed_audit(db_path, uow_id="uow-1", event="stall_detected",
                    ts="2026-01-02T10:00:00+00:00")

        results = stall_events(cutoff, registry_path=db_path)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# cycles_histogram
# ---------------------------------------------------------------------------

class TestCyclesHistogram:
    def test_counts_steward_cycle_events_per_uow(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        _seed_audit(db_path, uow_id="uow-1", event="steward_cycle", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_cycle", ts="2026-01-01T11:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_cycle", ts="2026-01-01T12:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-2", event="steward_cycle", ts="2026-01-01T10:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)

        assert result == {"uow-1": 3, "uow-2": 1}

    def test_excludes_non_steward_cycle_events(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        _seed_audit(db_path, uow_id="uow-1", event="status_change", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_cycle", ts="2026-01-01T11:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)

        assert result == {"uow-1": 1}

    def test_empty_dict_when_no_steward_cycles(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        result = cycles_histogram(registry_path=db_path)
        assert result == {}

    def test_returns_plain_dict(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        _seed_audit(db_path, uow_id="uow-1", event="steward_cycle", ts="2026-01-01T10:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)
        assert isinstance(result, dict)
        assert isinstance(list(result.values())[0], int)


# ---------------------------------------------------------------------------
# execution_outcomes
# ---------------------------------------------------------------------------

class TestExecutionOutcomes:
    def test_counts_outcomes_by_note_value(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for _ in range(3):
            _seed_audit(db_path, uow_id="uow-1", event="executor_outcome",
                        ts="2026-01-02T10:00:00+00:00", note="complete")
        for _ in range(2):
            _seed_audit(db_path, uow_id="uow-2", event="executor_outcome",
                        ts="2026-01-02T10:00:00+00:00", note="failed")
        _seed_audit(db_path, uow_id="uow-3", event="executor_outcome",
                    ts="2026-01-02T10:00:00+00:00", note="blocked")

        result = execution_outcomes(cutoff, registry_path=db_path)

        assert result == {"complete": 3, "failed": 2, "blocked": 1}

    def test_null_note_counted_as_unknown(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="executor_outcome",
                    ts="2026-01-02T10:00:00+00:00", note=None)

        result = execution_outcomes(cutoff, registry_path=db_path)

        assert result == {"unknown": 1}

    def test_excludes_events_before_since(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="executor_outcome",
                    ts="2026-01-01T10:00:00+00:00", note="complete")  # before cutoff
        _seed_audit(db_path, uow_id="uow-2", event="executor_outcome",
                    ts="2026-07-01T10:00:00+00:00", note="failed")    # after cutoff

        result = execution_outcomes(cutoff, registry_path=db_path)

        assert result == {"failed": 1}

    def test_excludes_non_executor_outcome_events(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-02T10:00:00+00:00", note="complete")

        result = execution_outcomes(cutoff, registry_path=db_path)
        assert result == {}

    def test_empty_dict_when_no_matching_events(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = execution_outcomes(cutoff, registry_path=db_path)
        assert result == {}
