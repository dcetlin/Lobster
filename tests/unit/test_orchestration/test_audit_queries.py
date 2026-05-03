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
    def test_counts_steward_activity_events_per_uow(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        # Seed with the actual event strings the steward writes
        _seed_audit(db_path, uow_id="uow-1", event="steward_prescription", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_diagnosis", ts="2026-01-01T11:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="agenda_update", ts="2026-01-01T12:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-2", event="steward_prescription", ts="2026-01-01T10:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)

        assert result == {"uow-1": 3, "uow-2": 1}

    def test_counts_all_steward_event_types(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        # Each of the seven steward event strings must be counted
        steward_events = [
            "steward_prescription",
            "steward_diagnosis",
            "steward_surface",
            "steward_closure",
            "agenda_update",
            "reentry_prescription",
            "prescription",
        ]
        for event in steward_events:
            _seed_audit(db_path, uow_id="uow-1", event=event, ts="2026-01-01T10:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)

        assert result == {"uow-1": len(steward_events)}

    def test_excludes_non_steward_events(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        _seed_audit(db_path, uow_id="uow-1", event="status_change", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="execution_complete", ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_prescription", ts="2026-01-01T11:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)

        assert result == {"uow-1": 1}

    def test_empty_dict_when_no_steward_events(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        result = cycles_histogram(registry_path=db_path)
        assert result == {}

    def test_returns_plain_dict(self, db_path):
        from src.orchestration.audit_queries import cycles_histogram

        _seed_audit(db_path, uow_id="uow-1", event="steward_prescription", ts="2026-01-01T10:00:00+00:00")

        result = cycles_histogram(registry_path=db_path)
        assert isinstance(result, dict)
        assert isinstance(list(result.values())[0], int)


# ---------------------------------------------------------------------------
# execution_outcomes
# ---------------------------------------------------------------------------

class TestExecutionOutcomes:
    def test_counts_execution_complete_and_failed_events(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes
        import json

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        # execution_complete — note is a JSON dict matching executor._complete_uow
        for _ in range(3):
            _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                        ts="2026-01-02T10:00:00+00:00",
                        note=json.dumps({"actor": "executor", "output_ref": "/tmp/out", "timestamp": "2026-01-02T10:00:00+00:00"}))
        # execution_failed — note is a JSON dict matching executor._fail_uow
        for _ in range(2):
            _seed_audit(db_path, uow_id="uow-2", event="execution_failed",
                        ts="2026-01-02T10:00:00+00:00",
                        note=json.dumps({"actor": "executor", "reason": "timeout", "timestamp": "2026-01-02T10:00:00+00:00"}))

        result = execution_outcomes(cutoff, registry_path=db_path)

        assert result == {"execution_complete": 3, "execution_failed": 2}

    def test_excludes_events_before_since(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes
        import json

        cutoff = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                    ts="2026-01-01T10:00:00+00:00",  # before cutoff
                    note=json.dumps({"actor": "executor", "output_ref": "/tmp/out", "timestamp": "2026-01-01T10:00:00+00:00"}))
        _seed_audit(db_path, uow_id="uow-2", event="execution_failed",
                    ts="2026-07-01T10:00:00+00:00",  # after cutoff
                    note=json.dumps({"actor": "executor", "reason": "timeout", "timestamp": "2026-07-01T10:00:00+00:00"}))

        result = execution_outcomes(cutoff, registry_path=db_path)

        assert result == {"execution_failed": 1}

    def test_excludes_non_executor_events(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-02T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_prescription",
                    ts="2026-01-02T10:00:00+00:00")

        result = execution_outcomes(cutoff, registry_path=db_path)
        assert result == {}

    def test_empty_dict_when_no_matching_events(self, db_path):
        from src.orchestration.audit_queries import execution_outcomes

        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = execution_outcomes(cutoff, registry_path=db_path)
        assert result == {}


# ---------------------------------------------------------------------------
# execution_attempts
# ---------------------------------------------------------------------------

class TestExecutionAttempts:
    def test_returns_execution_events_newest_first(self, db_path):
        from src.orchestration.audit_queries import execution_attempts

        _seed_audit(db_path, uow_id="uow-1", event="executor_dispatch",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="execution_failed",
                    ts="2026-01-01T11:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                    ts="2026-01-01T12:00:00+00:00")

        results = execution_attempts("uow-1", registry_path=db_path)
        assert len(results) == 3
        # Newest first by id
        assert results[0]["event"] == "execution_complete"
        assert results[-1]["event"] == "executor_dispatch"

    def test_excludes_non_execution_events(self, db_path):
        from src.orchestration.audit_queries import execution_attempts

        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                    ts="2026-01-01T11:00:00+00:00")

        results = execution_attempts("uow-1", registry_path=db_path)
        assert len(results) == 1
        assert results[0]["event"] == "execution_complete"

    def test_empty_for_uow_with_no_execution_events(self, db_path):
        from src.orchestration.audit_queries import execution_attempts

        results = execution_attempts("no-such-uow", registry_path=db_path)
        assert results == []

    def test_does_not_return_other_uow_events(self, db_path):
        from src.orchestration.audit_queries import execution_attempts

        _seed_audit(db_path, uow_id="uow-A", event="execution_complete",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-B", event="execution_complete",
                    ts="2026-01-01T10:00:00+00:00")

        results = execution_attempts("uow-A", registry_path=db_path)
        assert all(r["uow_id"] == "uow-A" for r in results)


# ---------------------------------------------------------------------------
# diagnosis_following_failure
# ---------------------------------------------------------------------------

class TestDiagnosisFollowingFailure:
    def test_empty_when_no_failures(self, db_path):
        from src.orchestration.audit_queries import diagnosis_following_failure

        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = diagnosis_following_failure(since, registry_path=db_path)
        assert result == []

    def test_failure_without_rediagnosis_excluded(self, db_path):
        from src.orchestration.audit_queries import diagnosis_following_failure

        _seed_audit(db_path, uow_id="uow-1", event="execution_failed",
                    ts="2026-01-01T10:00:00+00:00")
        # No subsequent diagnosis event
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = diagnosis_following_failure(since, registry_path=db_path)
        assert result == []

    def test_failure_with_steward_diagnosis_included(self, db_path):
        from src.orchestration.audit_queries import diagnosis_following_failure

        _seed_audit(db_path, uow_id="uow-1", event="execution_failed",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                    ts="2026-01-01T11:00:00+00:00")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = diagnosis_following_failure(since, registry_path=db_path)
        assert len(result) == 1
        entry = result[0]
        assert entry["uow_id"] == "uow-1"
        assert entry["gap_seconds"] == 3600.0

    def test_failure_with_reentry_prescription_included(self, db_path):
        from src.orchestration.audit_queries import diagnosis_following_failure

        _seed_audit(db_path, uow_id="uow-1", event="execution_failed",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="reentry_prescription",
                    ts="2026-01-01T10:30:00+00:00")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = diagnosis_following_failure(since, registry_path=db_path)
        assert len(result) == 1
        assert result[0]["gap_seconds"] == 1800.0

    def test_excludes_failures_before_since(self, db_path):
        from src.orchestration.audit_queries import diagnosis_following_failure

        _seed_audit(db_path, uow_id="uow-old", event="execution_failed",
                    ts="2025-12-31T23:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-old", event="steward_diagnosis",
                    ts="2026-01-01T00:30:00+00:00")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = diagnosis_following_failure(since, registry_path=db_path)
        assert result == []


# ---------------------------------------------------------------------------
# completed_uow_durations
# ---------------------------------------------------------------------------

class TestCompletedUowDurations:
    def _insert_done_uow(
        self,
        db_path: Path,
        uow_id: str,
        created_at: str,
        completed_at: str,
        steward_cycles: int = 1,
        lifetime_cycles: int = 0,
        register: str = "operational",
    ) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, source, status, summary, created_at, updated_at,
                 steward_cycles, lifetime_cycles, steward_log, success_criteria, completed_at, register)
            VALUES (?, ?, 'done', ?, ?, ?, ?, ?, '', '', ?, ?)
            """,
            (uow_id, "github:issue/1", "test", created_at, created_at,
             steward_cycles, lifetime_cycles, completed_at, register),
        )
        conn.commit()
        conn.close()

    def test_empty_when_no_completed_uows(self, db_path):
        from src.orchestration.audit_queries import completed_uow_durations

        result = completed_uow_durations("2026-01-01", registry_path=db_path)
        assert result == []

    def test_returns_completed_uow_with_duration(self, db_path):
        from src.orchestration.audit_queries import completed_uow_durations

        self._insert_done_uow(
            db_path, "uow-1",
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T02:00:00+00:00",
        )
        result = completed_uow_durations("2026-01-01", registry_path=db_path)
        assert len(result) == 1
        entry = result[0]
        assert entry["uow_id"] == "uow-1"
        assert entry["wall_clock_hours"] == 2.0
        assert entry["steward_cycles"] == 1

    def test_excludes_uows_completed_before_since(self, db_path):
        from src.orchestration.audit_queries import completed_uow_durations

        self._insert_done_uow(
            db_path, "uow-old",
            created_at="2025-12-31T00:00:00+00:00",
            completed_at="2025-12-31T01:00:00+00:00",
        )
        result = completed_uow_durations("2026-01-01", registry_path=db_path)
        assert result == []

    def test_register_field_included(self, db_path):
        from src.orchestration.audit_queries import completed_uow_durations

        self._insert_done_uow(
            db_path, "uow-1",
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T01:00:00+00:00",
            register="reflective",
        )
        result = completed_uow_durations("2026-01-01", registry_path=db_path)
        assert result[0]["register"] == "reflective"

    def test_lifetime_cycles_field_included(self, db_path):
        from src.orchestration.audit_queries import completed_uow_durations

        self._insert_done_uow(
            db_path, "uow-lc",
            created_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T03:00:00+00:00",
            steward_cycles=2,
            lifetime_cycles=7,
        )
        result = completed_uow_durations("2026-01-01", registry_path=db_path)
        assert len(result) == 1
        assert result[0]["lifetime_cycles"] == 7


# ---------------------------------------------------------------------------
# event_sequence
# ---------------------------------------------------------------------------

class TestEventSequence:
    def test_returns_full_sequence_oldest_first(self, db_path):
        from src.orchestration.audit_queries import event_sequence

        _seed_audit(db_path, uow_id="uow-1", event="status_change",
                    ts="2026-01-01T10:00:00+00:00", to_status="pending")
        _seed_audit(db_path, uow_id="uow-1", event="steward_diagnosis",
                    ts="2026-01-01T11:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                    ts="2026-01-01T12:00:00+00:00")

        result = event_sequence("uow-1", registry_path=db_path)
        assert len(result) == 3
        # Oldest first
        assert result[0]["event"] == "status_change"
        assert result[1]["event"] == "steward_diagnosis"
        assert result[2]["event"] == "execution_complete"

    def test_empty_for_unknown_uow(self, db_path):
        from src.orchestration.audit_queries import event_sequence

        result = event_sequence("no-such-uow", registry_path=db_path)
        assert result == []

    def test_does_not_return_other_uow_events(self, db_path):
        from src.orchestration.audit_queries import event_sequence

        _seed_audit(db_path, uow_id="uow-A", event="status_change",
                    ts="2026-01-01T10:00:00+00:00")
        _seed_audit(db_path, uow_id="uow-B", event="execution_complete",
                    ts="2026-01-01T10:00:00+00:00")

        result = event_sequence("uow-A", registry_path=db_path)
        assert all(r.get("uow_id") is None or True for r in result)
        # event_sequence only selects ts, event, from_status, to_status, agent, note
        assert len(result) == 1
        assert result[0]["event"] == "status_change"

    def test_returns_dict_with_expected_keys(self, db_path):
        from src.orchestration.audit_queries import event_sequence

        _seed_audit(db_path, uow_id="uow-1", event="execution_complete",
                    ts="2026-01-01T10:00:00+00:00")

        result = event_sequence("uow-1", registry_path=db_path)
        entry = result[0]
        assert set(entry.keys()) == {"ts", "event", "from_status", "to_status", "agent", "note"}
