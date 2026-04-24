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

      The gate tracks suppression state across calls (via a state file) so it can
      fire one-time inbox notifications on state change: once when suppression first
      activates, and once when it deactivates. This avoids multi-fire while ensuring
      the user has a visible signal at sustained suppression.

Usage in the prescription write step::

    from src.orchestration.wos_throttle import ConsumptionRateMonitor, PrescriptionThrottleGate

    monitor = ConsumptionRateMonitor()
    gate = PrescriptionThrottleGate(monitor)
    if gate.should_suppress_prescription():
        status = gate.gate_status()
        return f"[THROTTLED] Prescription suppressed: {status['reason']}"
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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


def _default_state_file() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")))
    return workspace / "data" / "throttle-gate-state.json"


def _admin_chat_id() -> str:
    return os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")


def _write_inbox_notification(text: str) -> None:
    """
    Write a system-originated notification to the Lobster inbox.

    Best-effort — failures are logged but do not gate throttle logic.
    The message type is 'system' so the dispatcher routes it as an
    informational notification rather than a wos_execute dispatch.
    """
    try:
        inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
        inbox_dir.mkdir(parents=True, exist_ok=True)
        msg_id = str(uuid.uuid4())
        msg: dict = {
            "id": msg_id,
            "source": "system",
            "type": "steward_trigger",
            "chat_id": _admin_chat_id(),
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = inbox_dir / f"{msg_id}.json.tmp"
        dest_path = inbox_dir / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "wos_throttle: failed to write inbox notification", exc_info=True
        )


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

    def get_rate(self, counts: dict[str, int] | None = None) -> float:
        """
        Return the consumption rate (closed / total) for the rolling window.

        Returns 1.0 (healthy) if there are no UoWs in the window, to avoid
        false throttling on a fresh install or after a long idle period.

        Pass *counts* (from _fetch_counts()) to reuse a pre-fetched snapshot
        and avoid a second DB read within the same check call.
        """
        if counts is None:
            counts = self._fetch_counts()
        closed = sum(counts.get(s, 0) for s in _CLOSED_STATUSES)
        open_ = sum(counts.get(s, 0) for s in _OPEN_STATUSES)
        total = closed + open_
        if total == 0:
            return 1.0
        return closed / total

    def is_backlog_critical(self, threshold: float = 0.6, counts: dict[str, int] | None = None) -> bool:
        """
        Return True when the consumption rate is below *threshold*.

        Default threshold 0.6: if fewer than 60% of UoWs opened in the last
        7 days are closed, the queue is growing faster than it is draining.

        Pass *counts* (from _fetch_counts()) to reuse a pre-fetched snapshot.
        """
        return self.get_rate(counts=counts) < threshold

    def backlog_depth(self, counts: dict[str, int] | None = None) -> int:
        """
        Return the number of open (non-terminal) UoWs in the rolling window.

        Pass *counts* (from _fetch_counts()) to reuse a pre-fetched snapshot
        and avoid a second DB read within the same check call.
        """
        if counts is None:
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

    State-change notifications: the gate writes a one-time inbox message when
    suppression first activates (False → True) and when it deactivates
    (True → False). State is persisted across process restarts via a JSON file
    in the workspace data directory. Notification failures are non-fatal.
    """

    def __init__(
        self,
        monitor: ConsumptionRateMonitor,
        threshold: float = 0.6,
        min_depth: int = 5,
        state_file: Path | None = None,
    ) -> None:
        self._monitor = monitor
        self._threshold = threshold
        self._min_depth = min_depth
        self._state_file = state_file or _default_state_file()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _read_last_suppressed(self) -> bool | None:
        """
        Return the last persisted suppression state, or None if unknown.
        """
        try:
            if not self._state_file.exists():
                return None
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return bool(data.get("suppressed"))
        except Exception:
            return None

    def _write_last_suppressed(self, suppressed: bool) -> None:
        """
        Persist the current suppression state (best-effort).
        """
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"suppressed": suppressed, "updated_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "wos_throttle: failed to write state file %s", self._state_file, exc_info=True
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_suppress_prescription(self) -> bool:
        """
        Return True to suppress UoW writes; False to allow them.

        Call this once per prescription batch (not per individual UoW).
        A single DB read (via _fetch_counts) is shared across the rate and
        depth checks to ensure both signals are computed from the same snapshot.

        Fires a one-time inbox notification on state change (suppression
        activates or deactivates) to give the user a visible signal.
        """
        counts = self._monitor._fetch_counts()
        suppressed = (
            self._monitor.is_backlog_critical(self._threshold, counts=counts)
            and self._monitor.backlog_depth(counts=counts) >= self._min_depth
        )
        self._maybe_notify(suppressed, counts)
        return suppressed

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

        Uses a single _fetch_counts() call so rate and depth are derived from
        the same DB snapshot (avoids two independent reads returning data from
        slightly different moments).
        """
        counts = self._monitor._fetch_counts()
        rate = self._monitor.get_rate(counts=counts)
        depth = self._monitor.backlog_depth(counts=counts)
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_notify(self, suppressed: bool, counts: dict[str, int]) -> None:
        """
        Fire a one-time inbox notification when suppression state changes.

        Transitions:
          None  → True   — first activation, notify
          False → True   — re-activation, notify
          True  → False  — deactivation, notify
          state unchanged — no-op
        """
        last = self._read_last_suppressed()
        if last == suppressed:
            return  # no state change, nothing to do

        self._write_last_suppressed(suppressed)
        rate = self._monitor.get_rate(counts=counts)
        depth = self._monitor.backlog_depth(counts=counts)

        if suppressed:
            text = (
                f"[THROTTLE GATE ACTIVATED] WOS prescription suppressed.\n"
                f"consumption_rate={rate:.2f} (threshold={self._threshold}), "
                f"backlog_depth={depth} (min_depth={self._min_depth}).\n"
                f"New GitHub issues will not be promoted to the WOS registry until "
                f"the rate recovers above {self._threshold}. "
                f"Check executor-side completion (decide_retry, decide_close handlers) "
                f"if rate stays low."
            )
        else:
            text = (
                f"[THROTTLE GATE DEACTIVATED] WOS prescription suppression lifted.\n"
                f"consumption_rate={rate:.2f} (threshold={self._threshold}), "
                f"backlog_depth={depth} (min_depth={self._min_depth}).\n"
                f"Cultivator will resume promoting GitHub issues to the WOS registry."
            )

        _write_inbox_notification(text)
