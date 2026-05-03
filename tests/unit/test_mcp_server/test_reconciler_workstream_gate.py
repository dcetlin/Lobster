"""
Unit tests for reconciler workstream status.md liveness gate.

Long-running dispatches write ~/lobster-workspace/workstreams/{task_id}/status.md
and update it every ~5 minutes. A status.md whose mtime is older than
WORKSTREAM_STATUS_STALE_SECONDS (10 min) is a reliable earlier stall signal
than the 15-minute output_file mtime gate.

Strategy: test the pure functions in session_store and the threshold-selection
logic mirrored from reconcile_agent_sessions(), without instantiating the async
server.
"""

import os
import time

import pytest


# ---------------------------------------------------------------------------
# Constants — mirror reconcile_agent_sessions() in inbox_server.py
# ---------------------------------------------------------------------------

DEFAULT_DEAD_THRESHOLD_SECONDS = 30 * 60
DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS = 120 * 60
MTIME_STALE_THRESHOLD_SECONDS = 15 * 60
WORKSTREAM_STATUS_STALE_SECONDS = 600  # 10 min


# ---------------------------------------------------------------------------
# Pure helpers — mirror the combined staleness logic in reconcile_agent_sessions()
# ---------------------------------------------------------------------------


def is_workstream_stale(ws_mtime: float | None, now_ts: float) -> bool:
    if ws_mtime is None:
        return False
    return (now_ts - ws_mtime) > WORKSTREAM_STATUS_STALE_SECONDS


def is_file_stale(output_mtime: float | None, now_ts: float) -> bool:
    if output_mtime is None:
        return False
    return (now_ts - output_mtime) > MTIME_STALE_THRESHOLD_SECONDS


def effective_running_threshold(
    ws_mtime: float | None,
    output_mtime: float | None,
    now_ts: float,
    timeout_minutes: int | None,
) -> int:
    ws_is_stale = is_workstream_stale(ws_mtime, now_ts)
    file_is_stale = is_file_stale(output_mtime, now_ts)
    effective_stale = ws_is_stale or file_is_stale

    if timeout_minutes is not None:
        dead_threshold_running = timeout_minutes * 60
        dead_threshold_missing = max(timeout_minutes * 60, DEFAULT_DEAD_THRESHOLD_SECONDS)
    else:
        dead_threshold_running = DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS
        dead_threshold_missing = DEFAULT_DEAD_THRESHOLD_SECONDS

    return dead_threshold_missing if effective_stale else dead_threshold_running


def should_kill_running_session(
    elapsed: int,
    ws_mtime: float | None,
    output_mtime: float | None,
    now_ts: float,
    timeout_minutes: int | None,
) -> bool:
    threshold = effective_running_threshold(ws_mtime, output_mtime, now_ts, timeout_minutes)
    return elapsed > threshold


# ---------------------------------------------------------------------------
# Tests: get_workstream_status_mtime (pure function in session_store)
# ---------------------------------------------------------------------------


class TestGetWorkstreamStatusMtime:
    """get_workstream_status_mtime returns mtime or None for missing/invalid paths."""

    def test_returns_none_when_task_id_empty(self):
        from agents.session_store import get_workstream_status_mtime
        assert get_workstream_status_mtime("") is None

    def test_returns_none_when_workstream_absent(self, tmp_path, monkeypatch):
        from agents import session_store
        monkeypatch.setattr(session_store, "WORKSTREAM_BASE", tmp_path)
        from agents.session_store import get_workstream_status_mtime
        assert get_workstream_status_mtime("no-such-task") is None

    def test_returns_mtime_when_status_md_present(self, tmp_path, monkeypatch):
        from agents import session_store
        monkeypatch.setattr(session_store, "WORKSTREAM_BASE", tmp_path)
        from agents.session_store import get_workstream_status_mtime

        ws_dir = tmp_path / "my-task"
        ws_dir.mkdir()
        status_file = ws_dir / "status.md"
        status_file.write_text("# status\ncurrent: working\n")

        result = get_workstream_status_mtime("my-task")
        assert isinstance(result, float)
        assert result > 0

    def test_returns_none_when_status_md_missing_but_dir_exists(self, tmp_path, monkeypatch):
        from agents import session_store
        monkeypatch.setattr(session_store, "WORKSTREAM_BASE", tmp_path)
        from agents.session_store import get_workstream_status_mtime

        ws_dir = tmp_path / "my-task"
        ws_dir.mkdir()
        # No status.md written

        assert get_workstream_status_mtime("my-task") is None

    def test_mtime_reflects_actual_write_time(self, tmp_path, monkeypatch):
        from agents import session_store
        monkeypatch.setattr(session_store, "WORKSTREAM_BASE", tmp_path)
        from agents.session_store import get_workstream_status_mtime

        ws_dir = tmp_path / "stale-task"
        ws_dir.mkdir()
        status_file = ws_dir / "status.md"
        status_file.write_text("old content\n")

        # Backdate mtime by 15 minutes
        old_time = time.time() - 15 * 60
        os.utime(str(status_file), (old_time, old_time))

        result = get_workstream_status_mtime("stale-task")
        assert result is not None
        assert abs(result - old_time) < 2


# ---------------------------------------------------------------------------
# Tests: workstream staleness detection
# ---------------------------------------------------------------------------


class TestWorkstreamStaleness:
    NOW = 1_000_000.0

    def test_fresh_status_md_is_not_stale(self):
        # Updated 3 minutes ago — well within 10-min threshold
        assert not is_workstream_stale(self.NOW - 3 * 60, self.NOW)

    def test_at_threshold_boundary_is_not_stale(self):
        # Exactly at threshold — strict > means not stale yet
        assert not is_workstream_stale(self.NOW - WORKSTREAM_STATUS_STALE_SECONDS, self.NOW)

    def test_just_over_threshold_is_stale(self):
        assert is_workstream_stale(self.NOW - WORKSTREAM_STATUS_STALE_SECONDS - 1, self.NOW)

    def test_old_status_md_is_stale(self):
        assert is_workstream_stale(self.NOW - 30 * 60, self.NOW)

    def test_none_mtime_is_not_stale(self):
        # No workstream dir — conservative fallback
        assert not is_workstream_stale(None, self.NOW)


# ---------------------------------------------------------------------------
# Tests: workstream gate fires earlier than output_file mtime gate
# ---------------------------------------------------------------------------


class TestWorkstreamGateEarlierSignal:
    """Workstream gate fires at 10 min; output_file gate fires at 15 min."""

    NOW = 1_000_000.0

    def test_workstream_stale_at_11min_triggers_before_output_file_gate(self):
        # status.md idle 11 min (> 10 min threshold)
        ws_mtime = self.NOW - 11 * 60
        # output_file fresh (updated 2 min ago)
        output_mtime = self.NOW - 2 * 60
        assert is_workstream_stale(ws_mtime, self.NOW)
        assert not is_file_stale(output_mtime, self.NOW)

    def test_output_file_gate_still_works_when_no_workstream(self):
        # No workstream (ws_mtime=None), output file idle 20 min
        output_mtime = self.NOW - 20 * 60
        assert not is_workstream_stale(None, self.NOW)
        assert is_file_stale(output_mtime, self.NOW)


# ---------------------------------------------------------------------------
# Tests: reconciler uses workstream signal when stale
# ---------------------------------------------------------------------------


class TestReconcilerWorkstreamGate:
    NOW = 1_000_000.0

    def test_workstream_stale_kills_at_35min(self):
        """
        status.md idle 12 min (>10 min) with fresh output_file.
        Workstream signal triggers short threshold → 35 min > 30 min → dead.
        """
        elapsed = 35 * 60
        ws_mtime = self.NOW - 12 * 60   # stale
        output_mtime = self.NOW - 2 * 60  # fresh
        assert should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_live_agent_with_fresh_status_md_survives_at_35min(self):
        """
        status.md updated 3 min ago, output_file fresh → both signals fresh.
        35 min < 120 min (long threshold) → alive.
        """
        elapsed = 35 * 60
        ws_mtime = self.NOW - 3 * 60    # fresh
        output_mtime = self.NOW - 2 * 60  # fresh
        assert not should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_falls_back_to_output_file_when_no_workstream(self):
        """
        No workstream (ws_mtime=None), output_file stale 20 min.
        Output_file gate fires → 35 min > 30 min → dead.
        """
        elapsed = 35 * 60
        ws_mtime = None               # no workstream dir
        output_mtime = self.NOW - 20 * 60  # stale
        assert should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_no_workstream_fresh_output_survives_at_35min(self):
        """
        No workstream, output_file fresh → long threshold applies.
        35 min < 120 min → alive.
        """
        elapsed = 35 * 60
        ws_mtime = None
        output_mtime = self.NOW - 2 * 60
        assert not should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_workstream_gate_below_threshold_does_not_fire(self):
        """
        status.md idle 8 min (< 10 min threshold) — not stale yet.
        35 min < 120 min long threshold → alive.
        """
        elapsed = 35 * 60
        ws_mtime = self.NOW - 8 * 60   # not yet stale
        output_mtime = self.NOW - 2 * 60
        assert not should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_both_signals_stale_kills_session(self):
        """Both workstream and output_file stale — session should be dead."""
        elapsed = 35 * 60
        ws_mtime = self.NOW - 15 * 60
        output_mtime = self.NOW - 20 * 60
        assert should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, None)

    def test_workstream_gate_does_not_affect_registered_long_timeout(self):
        """
        Agent registered for 240-minute timeout, status.md stale.
        Stale collapses to missing threshold = max(240*60, 30*60) = 240 min.
        35 min < 240 min → alive.
        """
        elapsed = 35 * 60
        ws_mtime = self.NOW - 12 * 60  # stale
        output_mtime = self.NOW - 2 * 60
        assert not should_kill_running_session(elapsed, ws_mtime, output_mtime, self.NOW, 240)
