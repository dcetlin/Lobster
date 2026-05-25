"""
Tests for waste_state_signal() in src/orchestration/analytics.py.

Uses an in-memory SQLite DB seeded with a minimal uow_registry schema.
All timestamps are set relative to "now" to fall within the 7-day window.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.analytics import waste_state_signal, _WASTE_ESCALATION_RATIO, _WASTE_MIN_SAMPLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _make_db(rows: list[dict]) -> Path:
    """
    Create an in-memory SQLite DB with uow_registry populated from rows.

    Each row dict may have: id, status, outcome_category, completed_at.
    Missing fields default to sensible values.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            status TEXT,
            outcome_category TEXT,
            completed_at TEXT,
            closed_at TEXT,
            updated_at TEXT
        )
        """
    )
    for i, row in enumerate(rows):
        conn.execute(
            """
            INSERT INTO uow_registry (id, status, outcome_category, completed_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                row.get("id", f"uow_{i:04d}"),
                row.get("status", "done"),
                row.get("outcome_category"),
                row.get("completed_at", _days_ago_iso(1)),
            ),
        )
    conn.commit()

    # Write to a temp file so _connect_ro (file:?mode=ro URI) can open it
    import tempfile, os, shutil
    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "registry.db"

    # Copy in-memory DB to file and enable WAL mode
    # (required because _connect_ro() executes PRAGMA journal_mode=WAL)
    file_conn = sqlite3.connect(str(db_path))
    conn.backup(file_conn)
    file_conn.execute("PRAGMA journal_mode=WAL")
    file_conn.close()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test 1: empty DB
# ---------------------------------------------------------------------------

def test_empty_db_returns_data_gap():
    db_path = _make_db([])
    result = waste_state_signal(db_path, window_days=7)

    assert result["accumulation_count"] == 0
    assert result["throughput_count"] == 0
    assert result["total_count"] == 0
    assert result["waste_ratio"] is None
    assert result["escalation_flag"] is False
    assert result["data_gap"] is not None
    assert "3" in result["data_gap"] or str(_WASTE_MIN_SAMPLE) in result["data_gap"]


# ---------------------------------------------------------------------------
# Test 2: all pearl/seed UoWs → waste_ratio < 1.0
# ---------------------------------------------------------------------------

def test_all_throughput_uows():
    rows = [
        {"id": f"uow_{i}", "status": "done", "outcome_category": "pearl", "completed_at": _days_ago_iso(1)}
        for i in range(4)
    ] + [
        {"id": f"seed_{i}", "status": "done", "outcome_category": "seed", "completed_at": _days_ago_iso(1)}
        for i in range(2)
    ]
    result = waste_state_signal(_make_db(rows), window_days=7)

    assert result["throughput_count"] == 6
    assert result["accumulation_count"] == 0
    assert result["waste_ratio"] == 0.0
    assert result["escalation_flag"] is False
    assert result["data_gap"] is None


# ---------------------------------------------------------------------------
# Test 3: all heat/shit UoWs → waste_ratio=None (no throughput)
# ---------------------------------------------------------------------------

def test_all_accumulation_uows():
    rows = [
        {"id": f"heat_{i}", "status": "done", "outcome_category": "heat", "completed_at": _days_ago_iso(1)}
        for i in range(3)
    ] + [
        {"id": f"shit_{i}", "status": "done", "outcome_category": "shit", "completed_at": _days_ago_iso(1)}
        for i in range(2)
    ]
    result = waste_state_signal(_make_db(rows), window_days=7)

    assert result["accumulation_count"] == 5
    assert result["throughput_count"] == 0
    assert result["waste_ratio"] is None
    assert result["escalation_flag"] is False  # no throughput → ratio None → no escalation


# ---------------------------------------------------------------------------
# Test 4: accumulation=10, throughput=4 → waste_ratio=2.5, escalation_flag=True
# ---------------------------------------------------------------------------

def test_high_waste_ratio_triggers_escalation():
    rows = (
        [{"id": f"acc_{i}", "status": "done", "outcome_category": "heat", "completed_at": _days_ago_iso(1)}
         for i in range(10)]
        + [{"id": f"thr_{i}", "status": "done", "outcome_category": "pearl", "completed_at": _days_ago_iso(1)}
           for i in range(4)]
    )
    result = waste_state_signal(_make_db(rows), window_days=7)

    assert result["accumulation_count"] == 10
    assert result["throughput_count"] == 4
    assert result["waste_ratio"] == pytest.approx(2.5, abs=0.001)
    assert result["escalation_flag"] is True
    assert result["escalation_reason"] is not None
    assert "2.5" in result["escalation_reason"] or "2.50" in result["escalation_reason"]


# ---------------------------------------------------------------------------
# Test 5: trend worsening when current ratio > prior ratio
# ---------------------------------------------------------------------------

def test_trend_worsening():
    # Current window (days 0-7): 6 accumulation, 3 throughput → ratio=2.0
    # Prior window (days 7-14): 3 accumulation, 6 throughput → ratio=0.5
    # Expect: trend = "worsening"
    current_rows = (
        [{"id": f"curr_acc_{i}", "status": "done", "outcome_category": "heat",
          "completed_at": _days_ago_iso(2)}
         for i in range(6)]
        + [{"id": f"curr_thr_{i}", "status": "done", "outcome_category": "pearl",
            "completed_at": _days_ago_iso(2)}
           for i in range(3)]
    )
    prior_rows = (
        [{"id": f"prior_acc_{i}", "status": "done", "outcome_category": "heat",
          "completed_at": _days_ago_iso(10)}
         for i in range(3)]
        + [{"id": f"prior_thr_{i}", "status": "done", "outcome_category": "pearl",
            "completed_at": _days_ago_iso(10)}
           for i in range(6)]
    )
    db_path = _make_db(current_rows + prior_rows)
    result = waste_state_signal(db_path, window_days=7)

    assert result["trend"] == "worsening"


# ---------------------------------------------------------------------------
# Test 6: failed/expired/cancelled statuses count as accumulation
# ---------------------------------------------------------------------------

def test_failed_statuses_count_as_accumulation():
    rows = [
        {"id": "f1", "status": "failed", "outcome_category": "pearl", "completed_at": _days_ago_iso(1)},
        {"id": "e1", "status": "expired", "outcome_category": None, "completed_at": _days_ago_iso(1)},
        {"id": "c1", "status": "cancelled", "outcome_category": "seed", "completed_at": _days_ago_iso(1)},
        {"id": "d1", "status": "done", "outcome_category": "pearl", "completed_at": _days_ago_iso(1)},
    ]
    result = waste_state_signal(_make_db(rows), window_days=7)

    # 3 failed/expired/cancelled → accumulation, 1 pearl+done → throughput
    assert result["accumulation_count"] == 3
    assert result["throughput_count"] == 1
