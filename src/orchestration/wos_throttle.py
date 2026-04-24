"""
WOS Prescription Throttle Gate
================================

Implements two components that work together to prevent unbounded UoW queue growth:

  ConsumptionRateMonitor
      Reads the WOS registry and computes the ratio of closed UoWs to opened UoWs
      over a configurable rolling window (default: 7 days).  Read-only — no mutations.

  PrescriptionThrottleGate
      Wraps a ConsumptionRateMonitor and decides whether the prescription engine
      should be suppressed.  Suppression fires when the backlog is both critical
      (rate < threshold) AND deep (depth >= min_depth).

Usage in the prescription write step::

    from src.orchestration.wos_throttle import ConsumptionRateMonitor, PrescriptionThrottleGate

    monitor = ConsumptionRateMonitor()
    gate = PrescriptionThrottleGate(monitor)
    if gate.should_suppress_prescription():
        status = gate.gate_status()
        return f"[THROTTLED] Prescription suppressed: {status['reason']}"
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path resolution — mirrors src/orchestration/paths.py without importing it
# (avoids circular imports if this module is loaded early)
# ---------------------------------------------------------------------------

import os

def _default_registry_db() -> Path:
    if os.environ.get("REGISTRY_DB_PATH"):
        return Path(os.environ["REGISTRY_DB_PATH"])
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")))
    return workspace / "orchestration" / "registry.db"


# Status buckets used for consumption rate calculation
_CLOSED_STATUSES = frozenset({"done", "cancelled", "expired", "failed"})
_OPEN_STATUSES = frozenset({"proposed", "ready-for-steward", "executing", "blocked", "needs-human-review"})


class ConsumptionRateMonitor:
    """
    Reads UoW state from the WOS registry and computes the consumption ratio.

    consumption_rate = closed_uows / (closed_uows + open_uows)
    over a configurable rolling window (default: last 7 days).

    A rate of 1.0 means all UoWs opened in the window are closed.
    A rate near 0.0 means the queue is filling faster than it is draining.

    All methods are read-only — no mutations are performed.
    """

    def __init__(
        self,
        registry_db: Path | None = None,
        window_days: int = 7,
    ) -> None:
        self._db = registry_db or _default_registry_db()
        self._window_days = window_days
        self._cutoff: str = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_rate(self) -> float:
        """
        Return the consumption rate (closed / total) for the rolling window.

        Returns 1.0 (healthy) if there are no UoWs in the window, to avoid
        false throttling on a fresh install or after a long idle period.
        """
        counts = self._fetch_counts()
        closed = sum(counts.get(s, 0) for s in _CLOSED_STATUSES)
        open_ = sum(counts.get(s, 0) for s in _OPEN_STATUSES)
        total = closed + open_
        if total == 0:
            return 1.0
        return closed / total

    def is_backlog_critical(self, threshold: float = 0.6) -> bool:
        """
        Return True when the consumption rate is below *threshold*.

        Default threshold 0.6: if fewer than 60% of UoWs opened in the last
        7 days are closed, the queue is growing faster than it is draining.
        """
        return self.get_rate() < threshold

    def backlog_depth(self) -> int:
        """
        Return the number of open (non-terminal) UoWs in the rolling window.
        """
        counts = self._fetch_counts()
        return sum(counts.get(s, 0) for s in _OPEN_STATUSES)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_counts(self) -> dict[str, int]:
        """
        Query the registry for UoW counts per status within the rolling window.
        Returns an empty dict on any error (fail-open: do not suppress).
        """
        if not self._db.exists():
            return {}
        try:
            conn = sqlite3.connect(str(self._db))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM uow_registry
                WHERE created_at >= ?
                GROUP BY status
                """,
                (self._cutoff,),
            )
            rows = cursor.fetchall()
            conn.close()
            return {row["status"]: row["cnt"] for row in rows}
        except (sqlite3.Error, OSError):
            # Fail-open: if we cannot read the registry, do not suppress
            return {}


class PrescriptionThrottleGate:
    """
    Decides whether the prescription write step should be suppressed.

    Suppression fires when ALL of the following are true:
      - monitor.is_backlog_critical(threshold) is True (rate < threshold)
      - monitor.backlog_depth() >= min_depth

    The dual condition prevents false positives on small queues:
    a rate of 0.0 with 2 open UoWs is not an emergency.
    """

    def __init__(
        self,
        monitor: ConsumptionRateMonitor,
        threshold: float = 0.6,
        min_depth: int = 5,
    ) -> None:
        self._monitor = monitor
        self._threshold = threshold
        self._min_depth = min_depth

    def should_suppress_prescription(self) -> bool:
        """
        Return True to suppress UoW writes; False to allow them.

        Call this once per prescription batch (not per individual UoW) — the
        check is idempotent but involves a DB read each call.
        """
        return (
            self._monitor.is_backlog_critical(self._threshold)
            and self._monitor.backlog_depth() >= self._min_depth
        )

    def gate_status(self) -> dict[str, Any]:
        """
        Return a snapshot dict for logging and diagnostics.

            {
                "suppressed": bool,
                "rate": float,
                "depth": int,
                "threshold": float,
                "min_depth": int,
                "reason": str,
            }
        """
        rate = self._monitor.get_rate()
        depth = self._monitor.backlog_depth()
        suppressed = (rate < self._threshold) and (depth >= self._min_depth)

        if suppressed:
            reason = (
                f"consumption_rate={rate:.2f} < threshold={self._threshold} "
                f"AND backlog_depth={depth} >= min_depth={self._min_depth}"
            )
        elif rate < self._threshold:
            reason = (
                f"rate critical ({rate:.2f} < {self._threshold}) "
                f"but depth={depth} < min_depth={self._min_depth} — not suppressing"
            )
        else:
            reason = f"healthy: rate={rate:.2f} >= threshold={self._threshold}"

        return {
            "suppressed": suppressed,
            "rate": rate,
            "depth": depth,
            "threshold": self._threshold,
            "min_depth": self._min_depth,
            "reason": reason,
        }
