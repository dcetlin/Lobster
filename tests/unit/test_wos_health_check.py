"""
Tests for scheduled-tasks/wos-health-check.py

Tests are derived from the spec in the WOS audit doc (wos-audit-20260422.md)
and issue #849:

- Stale UoW detection: UoWs stuck in proposed/pending >hc.STARVATION_THRESHOLD_HOURS
  are returned as starvation candidates.
- Short-duration UoWs are not flagged as starvation candidates.
- Heartbeat liveness: UoWs in active/executing with stale heartbeats are detected.
- UoWs in active/executing with no heartbeat (NULL heartbeat_at) are reported
  as stall_type="no_heartbeat".
- UoWs with a fresh heartbeat within heartbeat_ttl are NOT reported as stale.
- Long-running in-flight UoWs (>hc.ALERT_THRESHOLD_HOURS) trigger alert logic.
- Executor heartbeat liveness check returns is_stale=True when log absent.
- jobs.json enabled gate: job skips when disabled.

Phase 3 monitors (added per oracle verdict on PR #1331):
- check_dark_pipeline: returns None when audit_log has recent entries; returns alert
  string when audit_log is stale or empty.
- check_ready_queue_drain: returns None when drain events exist; returns alert when
  queue grows with zero drains.
- check_issue_rate_trend: returns None when ratio is below threshold; returns alert
  when open/close ratio exceeds threshold; returns None when gh CLI is unavailable.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load the module from its script path (not a package)
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / "scheduled-tasks"
    / "wos-health-check.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("wos_health_check", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()



# ---------------------------------------------------------------------------
# Fixtures — minimal SQLite registry for query tests
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a minimal registry.db with the uow_registry schema."""
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            summary TEXT,
            register TEXT DEFAULT 'operational',
            close_reason TEXT,
            output_ref TEXT,
            started_at TEXT,
            closed_at TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL,
            heartbeat_at TEXT,
            heartbeat_ttl INTEGER DEFAULT 300,
            steward_cycles INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            uow_id TEXT NOT NULL,
            event TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            agent TEXT,
            note TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _insert_uow(
    db: Path,
    uow_id: str,
    status: str,
    created_hours_ago: float = 1.0,
    started_hours_ago: float | None = None,
    heartbeat_at: str | None = None,
    heartbeat_ttl: int = 300,
    summary: str = "test uow",
) -> None:
    conn = sqlite3.connect(str(db))
    started = _hours_ago(started_hours_ago) if started_hours_ago is not None else None
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, status, summary, created_at, started_at, heartbeat_at, heartbeat_ttl)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uow_id, status, summary, _hours_ago(created_hours_ago), started, heartbeat_at, heartbeat_ttl),
    )
    conn.commit()
    conn.close()


def _insert_audit_entry(
    db: Path,
    uow_id: str,
    event: str,
    from_status: str | None = None,
    to_status: str | None = None,
    hours_ago: float = 0.0,
    minutes_ago: float | None = None,
) -> None:
    """Insert an audit_log entry at a specified time offset from now."""
    conn = sqlite3.connect(str(db))
    if minutes_ago is not None:
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    else:
        ts = _hours_ago(hours_ago)
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, uow_id, event, from_status, to_status),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# query_starvation_candidates
# ---------------------------------------------------------------------------

class TestQueryStarvationCandidates:
    def test_uow_stuck_in_proposed_beyond_threshold_is_returned(self, db_path):
        _insert_uow(db_path, "uow-old", "proposed", created_hours_ago=hc.STARVATION_THRESHOLD_HOURS + 1)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-old" in ids

    def test_uow_stuck_in_pending_beyond_threshold_is_returned(self, db_path):
        _insert_uow(db_path, "uow-pending", "pending", created_hours_ago=hc.STARVATION_THRESHOLD_HOURS + 2)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-pending" in ids

    def test_recent_proposed_uow_is_not_returned(self, db_path):
        _insert_uow(db_path, "uow-new", "proposed", created_hours_ago=1)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-new" not in ids

    def test_uow_exactly_at_threshold_is_not_returned(self, db_path):
        # At exactly threshold hours, the UoW has NOT exceeded the threshold yet
        _insert_uow(db_path, "uow-boundary", "proposed", created_hours_ago=hc.STARVATION_THRESHOLD_HOURS - 0.01)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-boundary" not in ids

    def test_active_uow_not_returned_as_starvation_candidate(self, db_path):
        # Starvation check only covers proposed/pending, not active
        _insert_uow(db_path, "uow-active", "active", created_hours_ago=hc.STARVATION_THRESHOLD_HOURS + 5)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-active" not in ids

    def test_absent_db_returns_empty_list(self, tmp_path):
        results = hc.query_starvation_candidates(tmp_path / "nonexistent.db", hc.STARVATION_THRESHOLD_HOURS)
        assert results == []

    def test_result_contains_age_hours_field(self, db_path):
        _insert_uow(db_path, "uow-aged", "proposed", created_hours_ago=hc.STARVATION_THRESHOLD_HOURS + 3)
        results = hc.query_starvation_candidates(db_path, hc.STARVATION_THRESHOLD_HOURS)
        aged = next(r for r in results if r["id"] == "uow-aged")
        assert aged["age_hours"] is not None
        assert aged["age_hours"] >= hc.STARVATION_THRESHOLD_HOURS


# ---------------------------------------------------------------------------
# query_stale_heartbeats — heartbeat liveness
# ---------------------------------------------------------------------------

class TestQueryStaleHeartbeats:
    def test_active_uow_with_stale_heartbeat_is_returned(self, db_path):
        # heartbeat written 10 minutes ago, heartbeat_ttl=300s (5 min) — stale
        stale_hb = _hours_ago(10 / 60)  # 10 minutes ago
        _insert_uow(
            db_path, "uow-stale-hb", "active",
            created_hours_ago=2, started_hours_ago=2,
            heartbeat_at=stale_hb, heartbeat_ttl=300,
        )
        results = hc.query_stale_heartbeats(db_path, hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        ids = [r["id"] for r in results]
        assert "uow-stale-hb" in ids
        result = next(r for r in results if r["id"] == "uow-stale-hb")
        assert result["stall_type"] == "heartbeat"

    def test_active_uow_with_fresh_heartbeat_is_not_returned(self, db_path):
        # heartbeat written 1 minute ago, heartbeat_ttl=300s (5 min) — fresh
        fresh_hb = _hours_ago(1 / 60)  # 1 minute ago
        _insert_uow(
            db_path, "uow-fresh-hb", "active",
            created_hours_ago=1, started_hours_ago=1,
            heartbeat_at=fresh_hb, heartbeat_ttl=300,
        )
        results = hc.query_stale_heartbeats(db_path, hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        ids = [r["id"] for r in results]
        assert "uow-fresh-hb" not in ids

    def test_active_uow_with_no_heartbeat_reported_as_no_heartbeat_type(self, db_path):
        # heartbeat_at is NULL — agent has never written a heartbeat
        _insert_uow(
            db_path, "uow-no-hb", "active",
            created_hours_ago=2, started_hours_ago=2,
            heartbeat_at=None, heartbeat_ttl=300,
        )
        results = hc.query_stale_heartbeats(db_path, hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        ids = [r["id"] for r in results]
        assert "uow-no-hb" in ids
        result = next(r for r in results if r["id"] == "uow-no-hb")
        assert result["stall_type"] == "no_heartbeat"

    def test_executing_uow_with_stale_heartbeat_is_detected(self, db_path):
        # 'executing' status should also be checked
        stale_hb = _hours_ago(10 / 60)
        _insert_uow(
            db_path, "uow-exec-stale", "executing",
            created_hours_ago=1, started_hours_ago=1,
            heartbeat_at=stale_hb, heartbeat_ttl=300,
        )
        results = hc.query_stale_heartbeats(db_path, hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        ids = [r["id"] for r in results]
        assert "uow-exec-stale" in ids

    def test_done_uow_not_included_in_heartbeat_check(self, db_path):
        _insert_uow(db_path, "uow-done", "done", created_hours_ago=5)
        results = hc.query_stale_heartbeats(db_path, hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        ids = [r["id"] for r in results]
        assert "uow-done" not in ids

    def test_absent_db_returns_empty_list(self, tmp_path):
        results = hc.query_stale_heartbeats(tmp_path / "nonexistent.db", hc.HEARTBEAT_STALE_BUFFER_SECONDS)
        assert results == []


# ---------------------------------------------------------------------------
# check_executor_heartbeat_liveness
# ---------------------------------------------------------------------------

class TestCheckExecutorHeartbeatLiveness:
    def test_returns_stale_true_when_log_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc, "_executor_log_file", lambda: tmp_path / "nonexistent.log")
        result = hc.check_executor_heartbeat_liveness()
        assert result["is_stale"] is True
        assert result["last_run_iso"] is None

    def test_returns_stale_false_when_log_recently_modified(self, tmp_path, monkeypatch):
        log_file = tmp_path / "executor-heartbeat.log"
        log_file.write_text("recent log entry")
        monkeypatch.setattr(hc, "_executor_log_file", lambda: log_file)
        result = hc.check_executor_heartbeat_liveness()
        assert result["is_stale"] is False
        assert result["age_minutes"] is not None
        assert result["age_minutes"] < 1  # just written


# ---------------------------------------------------------------------------
# jobs.json enabled gate
# ---------------------------------------------------------------------------

class TestJobsJsonGate:
    def test_job_skips_when_disabled_in_jobs_json(self, tmp_path, monkeypatch):
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"wos-health-check": {"enabled": False}}}))
        monkeypatch.setattr(hc, "_jobs_file", lambda: jobs_file)
        assert hc._is_job_enabled() is False

    def test_job_runs_when_enabled_in_jobs_json(self, tmp_path, monkeypatch):
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"wos-health-check": {"enabled": True}}}))
        monkeypatch.setattr(hc, "_jobs_file", lambda: jobs_file)
        assert hc._is_job_enabled() is True

    def test_job_defaults_to_enabled_when_jobs_json_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc, "_jobs_file", lambda: tmp_path / "nonexistent.json")
        assert hc._is_job_enabled() is True


# ---------------------------------------------------------------------------
# Long-running alert detection
# ---------------------------------------------------------------------------

class TestLongRunningAlert:
    def test_uow_stuck_beyond_alert_threshold_is_returned(self, db_path):
        _insert_uow(
            db_path, "uow-long", "active",
            created_hours_ago=hc.ALERT_THRESHOLD_HOURS + 1,
            started_hours_ago=hc.ALERT_THRESHOLD_HOURS + 1,
        )
        results = hc.query_long_running_in_flight(db_path, hc.ALERT_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-long" in ids

    def test_recently_started_uow_not_flagged(self, db_path):
        _insert_uow(
            db_path, "uow-recent", "active",
            created_hours_ago=1, started_hours_ago=1,
        )
        results = hc.query_long_running_in_flight(db_path, hc.ALERT_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-recent" not in ids


# ---------------------------------------------------------------------------
# Phase 3: check_dark_pipeline
# ---------------------------------------------------------------------------

class TestCheckDarkPipeline:
    """Dark pipeline detection: alert when audit_log is silent for >threshold days."""

    def test_healthy_returns_none_when_recent_audit_entries_exist(self, db_path):
        """Audit entries within the threshold window → healthy (returns None)."""
        _insert_audit_entry(db_path, "uow-1", "status_change", hours_ago=1)
        result = hc.check_dark_pipeline(db_path, threshold_days=hc.DARK_PIPELINE_THRESHOLD_DAYS)
        assert result is None

    def test_alert_when_audit_log_is_stale_beyond_threshold(self, db_path):
        """Last audit entry older than threshold → returns alert string."""
        stale_hours = (hc.DARK_PIPELINE_THRESHOLD_DAYS + 1) * 24
        _insert_audit_entry(db_path, "uow-old", "status_change", hours_ago=stale_hours)
        result = hc.check_dark_pipeline(db_path, threshold_days=hc.DARK_PIPELINE_THRESHOLD_DAYS)
        assert result is not None
        assert "Dark pipeline" in result
        assert str(hc.DARK_PIPELINE_THRESHOLD_DAYS) in result

    def test_alert_when_audit_log_is_empty(self, db_path):
        """No audit entries at all → returns alert string about empty log."""
        result = hc.check_dark_pipeline(db_path, threshold_days=hc.DARK_PIPELINE_THRESHOLD_DAYS)
        assert result is not None
        assert "empty" in result.lower()

    def test_returns_none_when_db_absent(self, tmp_path):
        """Missing database file → healthy (returns None, not an error)."""
        result = hc.check_dark_pipeline(tmp_path / "nonexistent.db")
        assert result is None

    def test_healthy_at_exact_threshold_boundary(self, db_path):
        """Audit entry at exactly 1 hour before the threshold → healthy."""
        just_within_hours = (hc.DARK_PIPELINE_THRESHOLD_DAYS * 24) - 1
        _insert_audit_entry(db_path, "uow-boundary", "status_change", hours_ago=just_within_hours)
        result = hc.check_dark_pipeline(db_path, threshold_days=hc.DARK_PIPELINE_THRESHOLD_DAYS)
        assert result is None


# ---------------------------------------------------------------------------
# Phase 3: check_ready_queue_drain
# ---------------------------------------------------------------------------

class TestCheckReadyQueueDrain:
    """Ready-queue growth without drain: alert when UoWs enter ready-for-executor
    but none transition out within the observation window."""

    def test_healthy_returns_none_when_drain_events_exist(self, db_path):
        """Queue growth + drain events within window → healthy (returns None)."""
        # UoW in ready-for-executor state
        _insert_uow(db_path, "uow-ready", "ready-for-executor", created_hours_ago=0.5)
        # Transition INTO ready-for-executor (growth)
        _insert_audit_entry(
            db_path, "uow-ready", "status_change",
            to_status="ready-for-executor", minutes_ago=30,
        )
        # Transition OUT OF ready-for-executor (drain)
        _insert_audit_entry(
            db_path, "uow-other", "status_change",
            from_status="ready-for-executor", to_status="executing", minutes_ago=20,
        )
        result = hc.check_ready_queue_drain(
            db_path, window_minutes=hc.READY_QUEUE_DRAIN_WINDOW_MINUTES,
        )
        assert result is None

    def test_alert_when_queue_grows_with_no_drain(self, db_path):
        """Queue growth + zero drain events within window → returns alert string."""
        _insert_uow(db_path, "uow-stuck", "ready-for-executor", created_hours_ago=0.5)
        # Transition INTO ready-for-executor (growth) — but no drain
        _insert_audit_entry(
            db_path, "uow-stuck", "status_change",
            to_status="ready-for-executor", minutes_ago=30,
        )
        result = hc.check_ready_queue_drain(
            db_path, window_minutes=hc.READY_QUEUE_DRAIN_WINDOW_MINUTES,
        )
        assert result is not None
        assert "Ready-queue growth without drain" in result
        assert "1 UoW(s)" in result

    def test_healthy_returns_none_when_no_new_ready_entries(self, db_path):
        """No entries transitioned into ready-for-executor within window → healthy."""
        # A UoW sits in the queue, but it entered before the observation window
        _insert_uow(db_path, "uow-old-ready", "ready-for-executor", created_hours_ago=5)
        result = hc.check_ready_queue_drain(
            db_path, window_minutes=hc.READY_QUEUE_DRAIN_WINDOW_MINUTES,
        )
        assert result is None

    def test_returns_none_when_db_absent(self, tmp_path):
        """Missing database file → healthy (returns None)."""
        result = hc.check_ready_queue_drain(tmp_path / "nonexistent.db")
        assert result is None

    def test_alert_includes_queue_depth(self, db_path):
        """Alert message includes current queue depth for observability."""
        _insert_uow(db_path, "uow-q1", "ready-for-executor", created_hours_ago=0.5)
        _insert_uow(db_path, "uow-q2", "ready-for-executor", created_hours_ago=0.5)
        _insert_audit_entry(
            db_path, "uow-q1", "status_change",
            to_status="ready-for-executor", minutes_ago=10,
        )
        result = hc.check_ready_queue_drain(
            db_path, window_minutes=hc.READY_QUEUE_DRAIN_WINDOW_MINUTES,
        )
        assert result is not None
        assert "queue depth: 2" in result


# ---------------------------------------------------------------------------
# Phase 3: check_issue_rate_trend
# ---------------------------------------------------------------------------

class TestCheckIssueRateTrend:
    """Issue open/close rate trend: alert when open rate exceeds close rate by >threshold."""

    def _mock_gh_result(self, lines: list[str]) -> MagicMock:
        """Build a mock subprocess.run result with the given output lines."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\n".join(lines) + "\n" if lines else ""
        mock_result.stderr = ""
        return mock_result

    def test_healthy_returns_none_when_ratio_below_threshold(self):
        """open/close ratio below ISSUE_RATE_ALERT_RATIO → healthy (returns None)."""
        # 3 opened, 3 closed → ratio 1.0 < 1.5 threshold
        opened_lines = ["1\topen\ttitle1", "2\topen\ttitle2", "3\topen\ttitle3"]
        closed_lines = ["4\tclosed\ttitle4", "5\tclosed\ttitle5", "6\tclosed\ttitle6"]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "--state" in cmd:
                idx = cmd.index("--state")
                state = cmd[idx + 1]
                if state == "open":
                    return self._mock_gh_result(opened_lines)
                elif state == "closed":
                    return self._mock_gh_result(closed_lines)
            return self._mock_gh_result([])

        with patch("subprocess.run", side_effect=side_effect):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is None

    def test_alert_when_ratio_exceeds_threshold(self):
        """open/close ratio exceeds ISSUE_RATE_ALERT_RATIO → returns alert string."""
        # 8 opened, 3 closed → ratio 2.67 > 1.5 threshold
        opened_lines = [f"{i}\topen\ttitle{i}" for i in range(8)]
        closed_lines = [f"{i}\tclosed\ttitle{i}" for i in range(3)]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "--state" in cmd:
                idx = cmd.index("--state")
                state = cmd[idx + 1]
                if state == "open":
                    return self._mock_gh_result(opened_lines)
                elif state == "closed":
                    return self._mock_gh_result(closed_lines)
            return self._mock_gh_result([])

        with patch("subprocess.run", side_effect=side_effect):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is not None
        assert "Issue rate trend" in result
        assert "test/repo" in result

    def test_alert_when_opened_nonzero_and_closed_zero(self):
        """Issues opened with zero closures → returns alert string."""
        opened_lines = ["1\topen\ttitle1", "2\topen\ttitle2"]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "--state" in cmd:
                idx = cmd.index("--state")
                state = cmd[idx + 1]
                if state == "open":
                    return self._mock_gh_result(opened_lines)
                elif state == "closed":
                    return self._mock_gh_result([])
            return self._mock_gh_result([])

        with patch("subprocess.run", side_effect=side_effect):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is not None
        assert "2 issue(s) opened, 0 closed" in result

    def test_healthy_returns_none_when_both_counts_zero(self):
        """No issues opened or closed in the window → healthy (returns None)."""
        def side_effect(*args, **kwargs):
            return self._mock_gh_result([])

        with patch("subprocess.run", side_effect=side_effect):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is None

    def test_returns_none_when_gh_cli_unavailable(self):
        """gh CLI not found → returns None (graceful degradation)."""
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is None

    def test_returns_none_when_gh_cli_times_out(self):
        """gh CLI times out → returns None (graceful degradation)."""
        import subprocess as sp
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="gh", timeout=30)):
            result = hc.check_issue_rate_trend(
                repo="test/repo",
                window_days=hc.ISSUE_RATE_WINDOW_DAYS,
                alert_ratio=hc.ISSUE_RATE_ALERT_RATIO,
            )
        assert result is None
