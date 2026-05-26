"""
prescription_metrics.py — Structured metrics logger for steward prescription events.

Provides PrescriptionMetricsLogger, which records two categories of steward
events for downstream analysis:

- log_prescription_generated: emitted each time the steward writes a new
  prescription for a UoW (once per dispatch cycle).
- log_uow_closed: emitted when a UoW transitions to Done/Failed and the
  steward records total lifecycle metrics.

Backing store: wos-metrics.db (separate from the registry DB to avoid write
contention). Tables are created lazily on first write. DB path is derived from
LOBSTER_WORKSPACE env var (default: ~/lobster-workspace/orchestration/wos-metrics.db).

All methods are non-fatal: exceptions are caught and logged at WARNING level
so the steward main loop is never interrupted by metrics collection failures.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("prescription_metrics")

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS prescription_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    uow_id TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    executor_type TEXT,
    has_minimum_viable_output INTEGER,
    has_boundary INTEGER,
    has_success_criteria_check INTEGER,
    estimated_cycles INTEGER,
    word_count INTEGER,
    step_count INTEGER,
    source_issue TEXT
);

CREATE TABLE IF NOT EXISTS closure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    uow_id TEXT NOT NULL,
    total_cycles INTEGER NOT NULL,
    closure_outcome TEXT NOT NULL,
    final_prescription_cycle INTEGER
);
"""


class PrescriptionMetricsLogger:
    """Logs prescription and closure events for WOS structural analysis.

    All methods are non-fatal: if logging fails the exception is caught and
    logged at WARNING level so the steward main loop is never interrupted.
    """

    def __init__(self) -> None:
        self._db_path: Path | None = None
        self._initialized = False
        self._lock = threading.Lock()

    def _db(self) -> Path:
        if self._db_path is None:
            workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
            self._db_path = Path(workspace) / "orchestration" / "wos-metrics.db"
        return self._db_path

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        db_path = self._db()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        if not self._initialized:
            self._ensure_schema(conn)
            self._initialized = True
        return conn

    def log_prescription_generated(
        self,
        *,
        uow_id: str,
        cycle: int,
        executor_type: str,
        has_minimum_viable_output: bool,
        has_boundary: bool,
        has_success_criteria_check: bool = False,
        estimated_cycles: int | None = None,
        word_count: int | None = None,
        step_count: int | None = None,
        source_issue: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Record that a prescription was generated for a UoW dispatch cycle.

        Args:
            uow_id: The UoW identifier.
            cycle: The dispatch cycle number at which this prescription was generated.
            executor_type: The selected executor type string (e.g. "functional-engineer").
            has_minimum_viable_output: Whether the prescription includes a
                "Minimum viable output" clause.
            has_boundary: Whether the prescription includes a "Boundary:" clause.
            has_success_criteria_check: Whether the UoW has non-empty success criteria.
            estimated_cycles: The estimated_cycles field value at prescription time.
            word_count: Word count of the generated instruction text.
            step_count: Number of "## " sections in the instruction text.
            source_issue: The originating GitHub issue URL or source field, if any.
            **kwargs: Reserved for future fields — accepted and ignored to allow
                call-site additions without breaking this signature.
        """
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """
                    INSERT INTO prescription_events (
                        recorded_at, uow_id, cycle, executor_type,
                        has_minimum_viable_output, has_boundary,
                        has_success_criteria_check, estimated_cycles,
                        word_count, step_count, source_issue
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(tz=timezone.utc).isoformat(),
                        uow_id,
                        cycle,
                        executor_type,
                        1 if has_minimum_viable_output else 0,
                        1 if has_boundary else 0,
                        1 if has_success_criteria_check else 0,
                        estimated_cycles,
                        word_count,
                        step_count,
                        source_issue,
                    ),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            log.warning("prescription_metrics write failed (prescription_events): %s", exc)

    def log_uow_closed(
        self,
        *,
        uow_id: str,
        total_cycles: int,
        closure_outcome: str,
        final_prescription_cycle: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Record UoW closure with final lifecycle metrics.

        Args:
            uow_id: The UoW identifier.
            total_cycles: Total number of dispatch cycles at closure.
            closure_outcome: Outcome label at closure (e.g. "success", "failed").
            final_prescription_cycle: The cycle number of the last generated
                prescription, if available.
            **kwargs: Reserved for future fields — accepted and ignored.
        """
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    """
                    INSERT INTO closure_events (
                        recorded_at, uow_id, total_cycles,
                        closure_outcome, final_prescription_cycle
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(tz=timezone.utc).isoformat(),
                        uow_id,
                        total_cycles,
                        closure_outcome,
                        final_prescription_cycle,
                    ),
                )
                conn.commit()
                conn.close()
        except Exception as exc:
            log.warning("prescription_metrics write failed (closure_events): %s", exc)
