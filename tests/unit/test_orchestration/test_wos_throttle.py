"""
Unit tests for src/orchestration/wos_throttle.py

Tests cover ConsumptionRateMonitor and PrescriptionThrottleGate using an
in-memory/tmp_path sqlite DB with a minimal uow_registry schema.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestration.wos_throttle import (
    ConsumptionRateMonitor,
    PrescriptionThrottleGate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """
    Create a minimal registry DB at tmp_path/registry.db with the given rows.
    Each row is (status, created_at) where created_at is a recent ISO timestamp
    so it falls within the default 7-day window.
    """
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE uow_registry (status TEXT, created_at TEXT)"
    )
    # Use a recent timestamp so rows are within the rolling window
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.executemany(
        "INSERT INTO uow_registry (status, created_at) VALUES (?, ?)",
        [(status, recent) for status, _ in rows],
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# ConsumptionRateMonitor tests
# ---------------------------------------------------------------------------

def test_empty_db_returns_healthy(tmp_path: Path) -> None:
    """get_rate() returns 1.0 when no rows exist."""
    db_path = _make_db(tmp_path, [])
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    assert monitor.get_rate() == 1.0


def test_absent_db_returns_healthy(tmp_path: Path) -> None:
    """get_rate() returns 1.0 when the db file does not exist."""
    db_path = tmp_path / "nonexistent.db"
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    assert monitor.get_rate() == 1.0


def test_rate_all_closed(tmp_path: Path) -> None:
    """5 'done' rows → rate = 1.0, is_backlog_critical(0.6) = False."""
    rows = [("done", "") for _ in range(5)]
    db_path = _make_db(tmp_path, rows)
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    assert monitor.get_rate() == 1.0
    assert monitor.is_backlog_critical(0.6) is False


def test_rate_mixed(tmp_path: Path) -> None:
    """3 'done', 7 'proposed' → rate = 0.3, is_backlog_critical(0.6) = True."""
    rows = [("done", "")] * 3 + [("proposed", "")] * 7
    db_path = _make_db(tmp_path, rows)
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    rate = monitor.get_rate()
    assert abs(rate - 0.3) < 1e-9
    assert monitor.is_backlog_critical(0.6) is True


# ---------------------------------------------------------------------------
# PrescriptionThrottleGate tests
# ---------------------------------------------------------------------------

def test_depth_gate_prevents_suppression_on_small_queue(tmp_path: Path) -> None:
    """rate = 0.3 but only 2 open UoWs → should_suppress_prescription() = False."""
    # 1 done, 2 proposed → rate = 1/3 ≈ 0.33 < 0.6, depth = 2 < min_depth=5
    rows = [("done", "")] * 1 + [("proposed", "")] * 2
    db_path = _make_db(tmp_path, rows)
    state_file = tmp_path / "state.json"
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    gate = PrescriptionThrottleGate(monitor, threshold=0.6, min_depth=5, state_file=state_file)
    with patch("orchestration.wos_throttle._write_inbox_notification"):
        assert gate.should_suppress_prescription() is False


def test_suppression_fires_when_both_conditions_met(tmp_path: Path) -> None:
    """rate < 0.6 AND depth >= 5 → should_suppress_prescription() = True."""
    # 3 done, 7 proposed → rate = 0.3 < 0.6, depth = 7 >= 5
    rows = [("done", "")] * 3 + [("proposed", "")] * 7
    db_path = _make_db(tmp_path, rows)
    state_file = tmp_path / "state.json"
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    gate = PrescriptionThrottleGate(monitor, threshold=0.6, min_depth=5, state_file=state_file)
    with patch("orchestration.wos_throttle._write_inbox_notification"):
        assert gate.should_suppress_prescription() is True


def test_no_notification_on_unchanged_state(tmp_path: Path) -> None:
    """Calling should_suppress_prescription() twice while suppressed fires only one state write."""
    rows = [("done", "")] * 3 + [("proposed", "")] * 7
    db_path = _make_db(tmp_path, rows)
    state_file = tmp_path / "state.json"
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    gate = PrescriptionThrottleGate(monitor, threshold=0.6, min_depth=5, state_file=state_file)
    with patch("orchestration.wos_throttle._write_inbox_notification") as mock_notify:
        gate.should_suppress_prescription()  # first call: state None → True, fires notification
        gate.should_suppress_prescription()  # second call: state True → True, no notification
    assert mock_notify.call_count == 1


def test_gate_status_fields(tmp_path: Path) -> None:
    """gate_status() returns dict with required keys."""
    db_path = _make_db(tmp_path, [])
    state_file = tmp_path / "state.json"
    monitor = ConsumptionRateMonitor(registry_db=db_path)
    gate = PrescriptionThrottleGate(monitor, state_file=state_file)
    status = gate.gate_status()
    required_keys = {"suppressed", "rate", "depth", "threshold", "min_depth", "reason"}
    assert required_keys.issubset(status.keys())
