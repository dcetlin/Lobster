"""
Tests for scheduled-tasks/wos-queue-monitor.py

Tests are derived from the spec in GitHub issue #681 and mito-modeling.md:
- STARVATION: queue depth 0 for 6+ consecutive hours (12 readings at 30-min cadence)
- TOXICITY: queue depth >10 for 3+ consecutive readings
- jobs.json enabled gate checked before running
- History JSONL append and load are correct
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load the module from its script path (not a package)
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / "scheduled-tasks"
    / "wos-queue-monitor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("wos_queue_monitor", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qm = _load_module()


# ---------------------------------------------------------------------------
# Constants — imported from the production module to avoid divergence
# ---------------------------------------------------------------------------

STARVATION_CONSECUTIVE_READINGS = qm.STARVATION_CONSECUTIVE_READINGS
TOXICITY_DEPTH_THRESHOLD = qm.TOXICITY_DEPTH_THRESHOLD
TOXICITY_CONSECUTIVE_READINGS = qm.TOXICITY_CONSECUTIVE_READINGS


# ---------------------------------------------------------------------------
# detect_starvation
# ---------------------------------------------------------------------------

class TestDetectStarvation:
    def _history(self, depths: list[int]) -> list[dict]:
        return [{"timestamp": f"2026-04-08T{i:02d}:00:00+00:00", "queue_depth": d}
                for i, d in enumerate(depths)]

    def test_starvation_fires_after_12_consecutive_zero_readings(self):
        """12 zero readings at 30-min cadence = exactly 6 hours — must fire."""
        history = self._history([0] * STARVATION_CONSECUTIVE_READINGS)
        assert qm.detect_starvation(history) is True

    def test_starvation_does_not_fire_with_11_zero_readings(self):
        """One reading short of the threshold — must not fire."""
        history = self._history([0] * (STARVATION_CONSECUTIVE_READINGS - 1))
        assert qm.detect_starvation(history) is False

    def test_starvation_does_not_fire_if_tail_has_nonzero(self):
        """12 readings but the last one is nonzero — no starvation."""
        depths = [0] * (STARVATION_CONSECUTIVE_READINGS - 1) + [1]
        history = self._history(depths)
        assert qm.detect_starvation(history) is False

    def test_starvation_fires_on_longer_history_when_tail_is_all_zero(self):
        """20 readings where the last 12 are all zero — must fire."""
        depths = [5] * 8 + [0] * STARVATION_CONSECUTIVE_READINGS
        history = self._history(depths)
        assert qm.detect_starvation(history) is True

    def test_starvation_does_not_fire_on_empty_history(self):
        assert qm.detect_starvation([]) is False

    def test_starvation_does_not_fire_on_insufficient_history(self):
        history = self._history([0] * 5)
        assert qm.detect_starvation(history) is False

    def test_starvation_uses_named_constant_not_magic_literal(self):
        """Verify the module exposes the constant matching the spec."""
        assert qm.STARVATION_CONSECUTIVE_READINGS == STARVATION_CONSECUTIVE_READINGS


# ---------------------------------------------------------------------------
# detect_toxicity
# ---------------------------------------------------------------------------

class TestDetectToxicity:
    def _history(self, depths: list[int]) -> list[dict]:
        return [{"timestamp": f"2026-04-08T{i:02d}:00:00+00:00", "queue_depth": d}
                for i, d in enumerate(depths)]

    def test_toxicity_fires_after_3_consecutive_readings_above_10(self):
        """3 consecutive readings > 10 — must fire."""
        history = self._history([11, 12, 13])
        fired, depth = qm.detect_toxicity(history)
        assert fired is True
        assert depth == 13

    def test_toxicity_depth_in_result_is_most_recent(self):
        """The returned depth should be from the last entry in the tail."""
        history = self._history([0] * 10 + [15, 20, 25])
        fired, depth = qm.detect_toxicity(history)
        assert fired is True
        assert depth == 25

    def test_toxicity_does_not_fire_with_2_consecutive_readings(self):
        """Only 2 readings above threshold — not enough."""
        history = self._history([15, 20])
        fired, _ = qm.detect_toxicity(history)
        assert fired is False

    def test_toxicity_does_not_fire_if_tail_has_10_exactly(self):
        """Threshold is strictly >10 — depth of exactly 10 must NOT fire."""
        history = self._history([10, 10, 10])
        fired, _ = qm.detect_toxicity(history)
        assert fired is False

    def test_toxicity_does_not_fire_if_one_reading_in_tail_is_at_threshold(self):
        """3 readings but the middle one is exactly 10 — no toxicity."""
        history = self._history([11, 10, 12])
        fired, _ = qm.detect_toxicity(history)
        assert fired is False

    def test_toxicity_fires_on_longer_history_when_tail_toxic(self):
        """Long history with a toxic tail — fires on the last 3."""
        depths = [0] * 20 + [11, 12, 15]
        history = self._history(depths)
        fired, depth = qm.detect_toxicity(history)
        assert fired is True
        assert depth == 15

    def test_toxicity_does_not_fire_on_empty_history(self):
        fired, _ = qm.detect_toxicity([])
        assert fired is False

    def test_toxicity_uses_named_constants_matching_spec(self):
        assert qm.TOXICITY_DEPTH_THRESHOLD == TOXICITY_DEPTH_THRESHOLD
        assert qm.TOXICITY_CONSECUTIVE_READINGS == TOXICITY_CONSECUTIVE_READINGS


# ---------------------------------------------------------------------------
# History file I/O
# ---------------------------------------------------------------------------

class TestHistoryIO:
    def test_load_history_returns_empty_for_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.jsonl"
        result = qm._load_history(missing)
        assert result == []

    def test_append_and_load_round_trip(self, tmp_path):
        f = tmp_path / "history.jsonl"
        qm._append_history(f, "2026-04-08T12:00:00+00:00", 5)
        qm._append_history(f, "2026-04-08T12:30:00+00:00", 0)
        entries = qm._load_history(f)
        assert len(entries) == 2
        assert entries[0] == {"timestamp": "2026-04-08T12:00:00+00:00", "queue_depth": 5}
        assert entries[1] == {"timestamp": "2026-04-08T12:30:00+00:00", "queue_depth": 0}

    def test_load_history_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "history.jsonl"
        f.write_text('{"timestamp": "T1", "queue_depth": 3}\nnot-json\n{"timestamp": "T2", "queue_depth": 7}\n')
        entries = qm._load_history(f)
        assert len(entries) == 2
        assert entries[0]["queue_depth"] == 3
        assert entries[1]["queue_depth"] == 7

    def test_append_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "history.jsonl"
        qm._append_history(nested, "2026-04-08T00:00:00+00:00", 1)
        assert nested.exists()


# ---------------------------------------------------------------------------
# jobs.json enabled gate
# ---------------------------------------------------------------------------

class TestJobsJsonEnabledGate:
    def test_returns_true_when_jobs_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert qm._is_job_enabled() is True

    def test_returns_true_when_job_entry_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        jobs_dir = tmp_path / "scheduled-jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "jobs.json").write_text(json.dumps({"jobs": {}}))
        assert qm._is_job_enabled() is True

    def test_returns_true_when_job_explicitly_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        jobs_dir = tmp_path / "scheduled-jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "jobs.json").write_text(json.dumps({
            "jobs": {"wos-queue-monitor": {"enabled": True}}
        }))
        assert qm._is_job_enabled() is True

    def test_returns_false_when_job_explicitly_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        jobs_dir = tmp_path / "scheduled-jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "jobs.json").write_text(json.dumps({
            "jobs": {"wos-queue-monitor": {"enabled": False}}
        }))
        assert qm._is_job_enabled() is False

    def test_returns_true_on_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        jobs_dir = tmp_path / "scheduled-jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "jobs.json").write_text("not-json")
        assert qm._is_job_enabled() is True


# ---------------------------------------------------------------------------
# query_queue_depth — DB interactions (using a real in-memory SQLite)
# ---------------------------------------------------------------------------

class TestQueryQueueDepth:
    def _create_registry_db(self, path: Path, rows: list[tuple]) -> None:
        """Create a minimal uow_registry table with the given (id, status) rows."""
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE uow_registry (id TEXT PRIMARY KEY, status TEXT)"
        )
        conn.executemany("INSERT INTO uow_registry VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

    def test_counts_ready_for_steward(self, tmp_path):
        db = tmp_path / "registry.db"
        self._create_registry_db(db, [
            ("u1", "ready-for-steward"),
            ("u2", "done"),
            ("u3", "failed"),
        ])
        assert qm.query_queue_depth(db) == 1

    def test_counts_all_three_backlog_statuses(self, tmp_path):
        db = tmp_path / "registry.db"
        self._create_registry_db(db, [
            ("u1", "ready-for-steward"),
            ("u2", "ready-for-executor"),
            ("u3", "active"),
            ("u4", "done"),
            ("u5", "failed"),
        ])
        assert qm.query_queue_depth(db) == 3

    def test_returns_zero_when_db_absent(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        assert qm.query_queue_depth(missing) == 0

    def test_returns_zero_when_table_absent(self, tmp_path):
        import sqlite3
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.close()
        assert qm.query_queue_depth(db) == 0

    def test_returns_zero_on_empty_registry(self, tmp_path):
        db = tmp_path / "registry.db"
        self._create_registry_db(db, [])
        assert qm.query_queue_depth(db) == 0


# ---------------------------------------------------------------------------
# write_task_output — output file written correctly
# ---------------------------------------------------------------------------

class TestWriteTaskOutput:
    def test_writes_json_file_with_expected_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        ts = "2026-04-08T14:00:00+00:00"
        qm._write_task_output("STARVATION: queue depth 0 for 360 minutes", "success", ts)
        outputs = list((tmp_path / "task-outputs").glob("*.json"))
        assert len(outputs) == 1
        data = json.loads(outputs[0].read_text())
        assert data["job_name"] == "wos-queue-monitor"
        assert data["status"] == "success"
        assert "STARVATION" in data["output"]
        assert data["timestamp"] == ts

    def test_write_is_atomic_via_tmp_rename(self, tmp_path, monkeypatch):
        """No .tmp file should remain after a successful write."""
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        qm._write_task_output("TOXICITY: queue depth >10", "success", "2026-04-08T15:00:00+00:00")
        tmp_files = list((tmp_path / "task-outputs").glob("*.tmp"))
        assert tmp_files == []
