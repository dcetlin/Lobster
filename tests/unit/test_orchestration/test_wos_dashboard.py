"""
Unit tests for wos_dashboard.py.

All Registry and audit_queries interactions are mocked — no real SQLite DB.
Tests cover:
- _active_uows: filters to active/ready-for-executor statuses, computes time_in_state
- _throughput_24h: delegates to execution_outcomes, maps key names
- _cycle_histogram_last_7d: groups by steward_cycles for completed UoWs
- _stalled_uows: filters by status + elapsed threshold
- _bootup_gate_status: calls is_bootup_candidate_gate_active()
- build_dashboard_data: assembles all sections into a single dict
- render_text: renders expected section headers and data
- render_text: empty states render '(none)' placeholders
- main(): exits 0, text format default
- main(): --format json outputs valid JSON
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_uow(
    id: str = "uow_20260101_aaa",
    status: str = "active",
    steward_cycles: int = 0,
    updated_at: str | None = None,
) -> MagicMock:
    """Create a mock UoW value object."""
    uow = MagicMock()
    uow.id = id
    uow.status = status
    uow.steward_cycles = steward_cycles
    # Default updated_at: 10 minutes ago
    if updated_at is None:
        updated_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    uow.updated_at = updated_at
    return uow


def _make_registry(uows: list[MagicMock] | None = None) -> MagicMock:
    """Create a mock Registry whose .list() returns the given UoWs."""
    registry = MagicMock()
    uows_list = uows or []
    registry.list.return_value = uows_list
    # registry.get(id) maps by id
    def _get(uow_id: str):
        for u in uows_list:
            if u.id == uow_id:
                return u
        return None
    registry.get.side_effect = _get
    return registry


# ---------------------------------------------------------------------------
# _active_uows
# ---------------------------------------------------------------------------

class TestActiveUows:
    def test_returns_active_uows(self):
        from src.orchestration.wos_dashboard import _active_uows
        uow = _make_uow(id="uow_1", status="active", steward_cycles=2)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert len(result) == 1
        assert result[0]["id"] == "uow_1"
        assert result[0]["status"] == "active"
        assert result[0]["steward_cycles"] == 2
        assert result[0]["time_in_state_seconds"] >= 0

    def test_returns_ready_for_executor(self):
        from src.orchestration.wos_dashboard import _active_uows
        uow = _make_uow(id="uow_2", status="ready-for-executor", steward_cycles=1)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert len(result) == 1
        assert result[0]["status"] == "ready-for-executor"

    def test_excludes_other_statuses(self):
        from src.orchestration.wos_dashboard import _active_uows
        uows = [
            _make_uow(id="uow_a", status="done"),
            _make_uow(id="uow_b", status="ready-for-steward"),
            _make_uow(id="uow_c", status="proposed"),
        ]
        registry = _make_registry(uows)
        result = _active_uows(registry)
        assert result == []

    def test_time_in_state_computed(self):
        from src.orchestration.wos_dashboard import _active_uows
        # UoW updated 1 hour ago
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        uow = _make_uow(id="uow_3", status="active", updated_at=one_hour_ago)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert result[0]["time_in_state_seconds"] >= 3590  # allow small margin

    def test_mixed_statuses_only_active_returned(self):
        from src.orchestration.wos_dashboard import _active_uows
        uows = [
            _make_uow(id="uow_active", status="active"),
            _make_uow(id="uow_rfe", status="ready-for-executor"),
            _make_uow(id="uow_done", status="done"),
        ]
        registry = _make_registry(uows)
        result = _active_uows(registry)
        returned_ids = {r["id"] for r in result}
        assert returned_ids == {"uow_active", "uow_rfe"}


# ---------------------------------------------------------------------------
# _throughput_24h
# ---------------------------------------------------------------------------

class TestThroughput24h:
    def test_delegates_to_execution_outcomes(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        fake_outcomes = {"execution_complete": 5, "execution_failed": 2}
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value=fake_outcomes,
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result == {"completed": 5, "failed": 2}

    def test_missing_keys_default_to_zero(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={},
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result == {"completed": 0, "failed": 0}

    def test_only_completed_key_present(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={"execution_complete": 3},
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result["completed"] == 3
        assert result["failed"] == 0


# ---------------------------------------------------------------------------
# _cycle_histogram_last_7d
# ---------------------------------------------------------------------------

class TestCycleHistogram:
    def test_groups_by_steward_cycles(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d

        uow_a = _make_uow(id="uow_a", steward_cycles=1)
        uow_b = _make_uow(id="uow_b", steward_cycles=2)
        uow_c = _make_uow(id="uow_c", steward_cycles=1)
        registry = _make_registry([uow_a, uow_b, uow_c])

        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=["uow_a", "uow_b", "uow_c"],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)

        assert result == {"cycles=1": 2, "cycles=2": 1}

    def test_empty_when_no_completions(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=[],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)
        assert result == {}

    def test_sorted_by_cycle_count(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d
        uow_a = _make_uow(id="uow_a", steward_cycles=3)
        uow_b = _make_uow(id="uow_b", steward_cycles=1)
        registry = _make_registry([uow_a, uow_b])
        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=["uow_a", "uow_b"],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)
        keys = list(result.keys())
        # Should be sorted ascending by cycle number
        assert keys == ["cycles=1", "cycles=3"]


# ---------------------------------------------------------------------------
# _stalled_uows
# ---------------------------------------------------------------------------

class TestStalledUows:
    def test_flags_ready_for_steward_over_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        # Updated 45 minutes ago — should be flagged
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        uow = _make_uow(id="uow_stale", status="ready-for-steward", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert len(result) == 1
        assert result[0]["id"] == "uow_stale"
        assert result[0]["time_in_state_seconds"] >= 2700

    def test_does_not_flag_under_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        # Updated 10 minutes ago — should NOT be flagged
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        uow = _make_uow(id="uow_fresh", status="ready-for-steward", updated_at=recent_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert result == []

    def test_flags_ready_for_executor_over_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        uow = _make_uow(id="uow_rfe_stale", status="ready-for-executor", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert len(result) == 1

    def test_ignores_active_status(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        uow = _make_uow(id="uow_active", status="active", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert result == []


# ---------------------------------------------------------------------------
# _bootup_gate_status
# ---------------------------------------------------------------------------

class TestBootupGateStatus:
    def test_gate_open_counts_ready_for_steward(self):
        from src.orchestration.wos_dashboard import _bootup_gate_status
        uow_rfs = _make_uow(id="uow_x", status="ready-for-steward")
        registry = _make_registry([uow_rfs])
        # list(status=...) should return UoWs in that status
        registry.list.side_effect = lambda status=None: (
            [uow_rfs] if status == "ready-for-steward" else []
        )
        with patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=True):
            result = _bootup_gate_status(registry)
        assert result["gate_open"] is True
        assert result["blocked_count"] == 1
        assert "OPEN" in result["description"]

    def test_gate_closed_reports_zero_blocked(self):
        from src.orchestration.wos_dashboard import _bootup_gate_status
        registry = _make_registry([])
        registry.list.side_effect = lambda status=None: []
        with patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False):
            result = _bootup_gate_status(registry)
        assert result["gate_open"] is False
        assert result["blocked_count"] == 0
        assert "CLOSED" in result["description"]


# ---------------------------------------------------------------------------
# build_dashboard_data
# ---------------------------------------------------------------------------

class TestBuildDashboardData:
    def test_contains_all_sections(self, tmp_path):
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=True):
            data = build_dashboard_data(registry, db_path)

        assert "generated_at" in data
        assert "active_uows" in data
        assert "throughput_24h" in data
        assert "cycle_histogram_7d" in data
        assert "stalled_uows" in data
        assert "bootup_candidate_gate" in data

    def test_generated_at_is_iso_utc(self, tmp_path):
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False):
            data = build_dashboard_data(registry, db_path)

        # Should parse without error
        ts = datetime.fromisoformat(data["generated_at"])
        assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------

class TestRenderText:
    def _empty_data(self) -> dict:
        return {
            "generated_at": "2026-03-30T12:00:00+00:00",
            "active_uows": [],
            "throughput_24h": {"completed": 0, "failed": 0},
            "cycle_histogram_7d": {},
            "stalled_uows": [],
            "bootup_candidate_gate": {
                "gate_open": False,
                "blocked_count": 0,
                "description": "gate is CLOSED — all UoWs are processed normally",
            },
        }

    def test_has_all_sections(self):
        from src.orchestration.wos_dashboard import render_text
        text = render_text(self._empty_data())
        assert "[1] Active UoWs" in text
        assert "[2] Throughput" in text
        assert "[3] Steward-cycle distribution" in text
        assert "[4] Active stalls" in text
        assert "[5] BOOTUP_CANDIDATE_GATE" in text

    def test_empty_active_shows_none(self):
        from src.orchestration.wos_dashboard import render_text
        text = render_text(self._empty_data())
        assert "(none)" in text

    def test_active_uow_shown_in_text(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["active_uows"] = [{
            "id": "uow_20260101_abc",
            "status": "active",
            "steward_cycles": 3,
            "time_in_state_seconds": 120,
        }]
        text = render_text(data)
        assert "uow_20260101_abc" in text
        assert "cycles=3" in text

    def test_stall_shown_in_text(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["stalled_uows"] = [{
            "id": "uow_stalled_x",
            "status": "ready-for-steward",
            "time_in_state_seconds": 2700,
        }]
        text = render_text(data)
        assert "STALLED" in text
        assert "uow_stalled_x" in text

    def test_throughput_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["throughput_24h"] = {"completed": 7, "failed": 2}
        text = render_text(data)
        assert "completed: 7" in text
        assert "failed: 2" in text

    def test_histogram_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["cycle_histogram_7d"] = {"cycles=1": 3, "cycles=2": 5}
        text = render_text(data)
        assert "cycles=1: 3" in text
        assert "cycles=2: 5" in text

    def test_gate_open_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["bootup_candidate_gate"] = {
            "gate_open": True,
            "blocked_count": 4,
            "description": "gate is OPEN — bootup-candidate UoWs are skipped by the Steward",
        }
        text = render_text(data)
        assert "OPEN" in text
        assert "4" in text


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------

class TestMain:
    def _patch_all(self, tmp_path: Path):
        """Context manager stack: patch Registry + all data sources."""
        from contextlib import ExitStack
        stack = ExitStack()
        registry = _make_registry([])
        registry.list.return_value = []

        stack.enter_context(patch(
            "src.orchestration.registry.Registry",
            return_value=registry,
        ))
        stack.enter_context(patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={},
        ))
        stack.enter_context(patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=[],
        ))
        stack.enter_context(patch(
            "src.orchestration.steward.BOOTUP_CANDIDATE_GATE",
            False,
        ))
        return stack

    def test_exits_zero_text(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "WOS Dashboard" in out

    def test_exits_zero_json(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db), "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "generated_at" in parsed
        assert "active_uows" in parsed

    def test_default_format_is_text(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        # Text output has section headers, not JSON
        assert "[1]" in out
