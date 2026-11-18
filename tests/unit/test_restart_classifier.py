"""
Unit tests for the restart type discriminator (issue #1561).

The classifier resolves the conflicting-signal problem where both a
session_reconnect (MCP server says "lightweight reconnect, no catchup")
and a compact-reminder (on-fresh-start.py says "catchup needed") land
in the same WFM cycle, with no structural way for the dispatcher to
pick one over the other.

Three restart types:
  COMPACTION        — compact-catchup IS needed
  SESSION_ROTATION  — compact-catchup is NOT needed
  FRESH_RESTART     — compact-catchup IS needed
"""

import pytest

from src.utils.restart_classifier import RestartType, classify_restart


class TestRestartTypeValues:
    """RestartType enum has the expected members and string values."""

    def test_compaction_value(self):
        assert RestartType.COMPACTION == "compaction"

    def test_session_rotation_value(self):
        assert RestartType.SESSION_ROTATION == "session_rotation"

    def test_fresh_restart_value(self):
        assert RestartType.FRESH_RESTART == "fresh_restart"

    def test_exactly_three_members(self):
        assert len(RestartType) == 3


class TestClassifyRestart:
    """classify_restart returns the correct type for each signal combination."""

    # --- Rule 1: compaction always wins ---

    def test_compaction_cause_without_session_reconnect(self):
        result = classify_restart(
            startup_cause="compaction",
            session_reconnect_queued=False,
        )
        assert result is RestartType.COMPACTION

    def test_compaction_cause_with_session_reconnect(self):
        """Compaction wins even when session_reconnect is present."""
        result = classify_restart(
            startup_cause="compaction",
            session_reconnect_queued=True,
        )
        assert result is RestartType.COMPACTION

    # --- Rule 2: session_reconnect → SESSION_ROTATION when not compaction ---

    def test_restart_cause_with_session_reconnect(self):
        """session_reconnect in inbox → SESSION_ROTATION (no catchup)."""
        result = classify_restart(
            startup_cause="restart",
            session_reconnect_queued=True,
        )
        assert result is RestartType.SESSION_ROTATION

    # --- Rule 3: otherwise → FRESH_RESTART ---

    def test_restart_cause_without_session_reconnect(self):
        result = classify_restart(
            startup_cause="restart",
            session_reconnect_queued=False,
        )
        assert result is RestartType.FRESH_RESTART

    def test_empty_startup_cause_without_session_reconnect(self):
        """Unknown/empty cause defaults to FRESH_RESTART."""
        result = classify_restart(
            startup_cause="",
            session_reconnect_queued=False,
        )
        assert result is RestartType.FRESH_RESTART

    def test_unknown_startup_cause_with_session_reconnect(self):
        """Unknown cause + session_reconnect → SESSION_ROTATION."""
        result = classify_restart(
            startup_cause="unknown",
            session_reconnect_queued=True,
        )
        assert result is RestartType.SESSION_ROTATION


class TestClassifyRestartIsPure:
    """classify_restart is a pure function: same inputs → same outputs."""

    SIGNAL_COMBINATIONS = [
        {"startup_cause": "compaction", "session_reconnect_queued": False},
        {"startup_cause": "compaction", "session_reconnect_queued": True},
        {"startup_cause": "restart", "session_reconnect_queued": False},
        {"startup_cause": "restart", "session_reconnect_queued": True},
        {"startup_cause": "", "session_reconnect_queued": False},
    ]

    @pytest.mark.parametrize("kwargs", SIGNAL_COMBINATIONS)
    def test_deterministic(self, kwargs):
        """Same inputs always produce the same RestartType."""
        result1 = classify_restart(**kwargs)
        result2 = classify_restart(**kwargs)
        assert result1 is result2
