"""Tests for PrescriptionMetricsLogger SQLite backing."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.orchestration.prescription_metrics import PrescriptionMetricsLogger


def _make_logger(tmp_path: Path) -> PrescriptionMetricsLogger:
    logger = PrescriptionMetricsLogger()
    logger._db_path = tmp_path / "test-metrics.db"
    return logger


def test_log_prescription_generated_writes_row(tmp_path: Path) -> None:
    logger = _make_logger(tmp_path)
    logger.log_prescription_generated(
        uow_id="uow_test_001",
        cycle=1,
        executor_type="functional-engineer",
        has_minimum_viable_output=True,
        has_boundary=True,
        has_success_criteria_check=False,
        estimated_cycles=2,
        word_count=150,
        step_count=4,
        source_issue="github:issue/578",
    )
    conn = sqlite3.connect(str(tmp_path / "test-metrics.db"))
    rows = conn.execute("SELECT * FROM prescription_events").fetchall()
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == "uow_test_001"  # uow_id
    assert row[3] == 1              # cycle
    assert row[4] == "functional-engineer"  # executor_type
    assert row[5] == 1              # has_minimum_viable_output
    assert row[6] == 1              # has_boundary
    assert row[7] == 0              # has_success_criteria_check
    assert row[8] == 2              # estimated_cycles
    assert row[9] == 150            # word_count
    assert row[10] == 4             # step_count
    assert row[11] == "github:issue/578"  # source_issue


def test_log_uow_closed_writes_row(tmp_path: Path) -> None:
    logger = _make_logger(tmp_path)
    logger.log_uow_closed(
        uow_id="uow_test_002",
        total_cycles=3,
        closure_outcome="success",
        final_prescription_cycle=2,
    )
    conn = sqlite3.connect(str(tmp_path / "test-metrics.db"))
    rows = conn.execute("SELECT * FROM closure_events").fetchall()
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == "uow_test_002"  # uow_id
    assert row[3] == 3               # total_cycles
    assert row[4] == "success"       # closure_outcome
    assert row[5] == 2               # final_prescription_cycle


def test_non_fatal_on_bad_path() -> None:
    logger = PrescriptionMetricsLogger()
    logger._db_path = Path("/nonexistent/dir/metrics.db")
    # Must not raise
    logger.log_prescription_generated(
        uow_id="uow_bad",
        cycle=1,
        executor_type="functional-engineer",
        has_minimum_viable_output=False,
        has_boundary=False,
    )
    logger.log_uow_closed(
        uow_id="uow_bad",
        total_cycles=1,
        closure_outcome="failed",
    )


def test_multiple_rows_accumulate(tmp_path: Path) -> None:
    logger = _make_logger(tmp_path)
    for i in range(3):
        logger.log_prescription_generated(
            uow_id=f"uow_multi_{i}",
            cycle=i + 1,
            executor_type="functional-engineer",
            has_minimum_viable_output=True,
            has_boundary=False,
        )
    conn = sqlite3.connect(str(tmp_path / "test-metrics.db"))
    count = conn.execute("SELECT COUNT(*) FROM prescription_events").fetchone()[0]
    conn.close()
    assert count == 3
