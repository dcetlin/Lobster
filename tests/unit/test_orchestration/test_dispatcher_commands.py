"""
Tests for the /quota, /status, and /help dispatcher commands.

These test pure handler functions — no Telegram, MCP, or network calls required.
All handlers return formatted strings derived from file reads or in-memory data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.orchestration.dispatcher_handlers import (
    COMMAND_HELP,
    handle_help,
    read_quota_state,
    format_quota_message,
    format_status_message,
)


# ---------------------------------------------------------------------------
# Constants matching the spec requirements
# ---------------------------------------------------------------------------

# Stale threshold in hours: data older than this is treated as unavailable.
# Matches the poller's 30-minute schedule — 2 hours gives headroom for gaps.
QUOTA_STALE_THRESHOLD_HOURS = 2


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_state(tmp_path: Path) -> Path:
    """Write a fresh cc-budget state.json and return the path."""
    state = {
        "v": 1,
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "rate_limits": {
            "five_hour": {
                "pct": 42.0,
                "resets_at": "2026-05-15T21:10:00.000000+00:00",
            },
            "seven_day": {
                "pct": 15.0,
                "resets_at": "2026-05-22T16:00:00.000000+00:00",
            },
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source": "cc-usage-poller",
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return p


@pytest.fixture
def stale_state(tmp_path: Path) -> Path:
    """Write a state.json with a last_updated timestamp 3 hours ago."""
    from datetime import timedelta

    old_ts = datetime.now(timezone.utc) - timedelta(hours=3)
    state = {
        "v": 1,
        "ts": int(old_ts.timestamp()),
        "rate_limits": {
            "five_hour": {"pct": 55.0, "resets_at": "2026-05-15T21:10:00+00:00"},
            "seven_day": {"pct": 20.0, "resets_at": "2026-05-22T16:00:00+00:00"},
        },
        "last_updated": old_ts.isoformat(),
        "source": "cc-usage-poller",
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return p


@pytest.fixture
def missing_state(tmp_path: Path) -> Path:
    """Return a path where no state.json file exists."""
    return tmp_path / "state.json"


# ---------------------------------------------------------------------------
# read_quota_state — pure reader, returns dict or None
# ---------------------------------------------------------------------------


class TestReadQuotaState:
    """read_quota_state reads ~/.claude/cc-budget/state.json (or override path)."""

    def test_returns_dict_for_valid_file(self, fresh_state: Path) -> None:
        """Valid state.json with rate_limits returns a parsed dict."""
        result = read_quota_state(state_path=fresh_state)
        assert result is not None
        assert isinstance(result, dict)

    def test_extracts_five_hour_pct(self, fresh_state: Path) -> None:
        """five_hour.pct must be accessible in the returned dict."""
        result = read_quota_state(state_path=fresh_state)
        assert result["rate_limits"]["five_hour"]["pct"] == 42.0

    def test_extracts_seven_day_pct(self, fresh_state: Path) -> None:
        """seven_day.pct must be accessible in the returned dict."""
        result = read_quota_state(state_path=fresh_state)
        assert result["rate_limits"]["seven_day"]["pct"] == 15.0

    def test_returns_none_for_missing_file(self, missing_state: Path) -> None:
        """Returns None (not an exception) when the file is absent."""
        result = read_quota_state(state_path=missing_state)
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        """Returns None when state.json contains invalid JSON."""
        p = tmp_path / "state.json"
        p.write_text("{ not valid json }")
        result = read_quota_state(state_path=p)
        assert result is None

    def test_returns_none_for_missing_rate_limits_key(self, tmp_path: Path) -> None:
        """Returns None when 'rate_limits' key is absent from state.json."""
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"v": 1, "ts": 1234567890}))
        result = read_quota_state(state_path=p)
        assert result is None


# ---------------------------------------------------------------------------
# format_quota_message — pure formatter, returns human-readable string
# ---------------------------------------------------------------------------


class TestFormatQuotaMessage:
    """format_quota_message formats the quota state dict into a Telegram message."""

    def test_returns_string(self, fresh_state: Path) -> None:
        state = read_quota_state(state_path=fresh_state)
        assert isinstance(format_quota_message(state), str)

    def test_includes_five_hour_percentage(self, fresh_state: Path) -> None:
        """Five-hour utilization percentage must appear in the output."""
        state = read_quota_state(state_path=fresh_state)
        msg = format_quota_message(state)
        assert "42" in msg

    def test_includes_seven_day_percentage(self, fresh_state: Path) -> None:
        """Seven-day utilization percentage must appear in the output."""
        state = read_quota_state(state_path=fresh_state)
        msg = format_quota_message(state)
        assert "15" in msg

    def test_includes_resets_label(self, fresh_state: Path) -> None:
        """Output must mention when the quota resets."""
        state = read_quota_state(state_path=fresh_state)
        msg = format_quota_message(state)
        assert "reset" in msg.lower()

    def test_unavailable_message_for_none_state(self) -> None:
        """None state (missing/unreadable file) produces the unavailable message."""
        msg = format_quota_message(None)
        assert "unavailable" in msg.lower()

    def test_unavailable_message_for_stale_state(self, stale_state: Path) -> None:
        """State older than QUOTA_STALE_THRESHOLD_HOURS triggers unavailable message."""
        state = read_quota_state(state_path=stale_state)
        msg = format_quota_message(state)
        assert "unavailable" in msg.lower()

    def test_quota_prefix_present(self, fresh_state: Path) -> None:
        """Output should begin with a 'CC usage' label for clarity on mobile."""
        state = read_quota_state(state_path=fresh_state)
        msg = format_quota_message(state)
        assert "CC usage" in msg or "cc usage" in msg.lower()


# ---------------------------------------------------------------------------
# format_status_message — pure formatter, returns system snapshot string
# ---------------------------------------------------------------------------


class TestFormatStatusMessage:
    """format_status_message assembles a snapshot from agents, WOS config, and quota."""

    def test_returns_string(self, fresh_state: Path) -> None:
        state = read_quota_state(state_path=fresh_state)
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": True},
            status_counts={},
            quota_state=state,
        )
        assert isinstance(result, str)

    def test_includes_wos_execution_state_true(self, fresh_state: Path) -> None:
        """WOS execution_enabled=True must appear in the output."""
        state = read_quota_state(state_path=fresh_state)
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": True},
            status_counts={},
            quota_state=state,
        )
        assert "true" in result.lower() or "enabled" in result.lower()

    def test_includes_wos_execution_state_false(self) -> None:
        """WOS execution_enabled=False must appear in the output."""
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": False},
            status_counts={},
            quota_state=None,
        )
        assert "false" in result.lower() or "disabled" in result.lower() or "stopped" in result.lower()

    def test_includes_agent_count_when_agents_present(self) -> None:
        """Active agent count (2) must appear when agents are running."""
        sessions = [
            {"agent_id": "task-a", "description": "First task"},
            {"agent_id": "task-b", "description": "Second task"},
        ]
        result = format_status_message(
            active_sessions=sessions,
            wos_config={"execution_enabled": True},
            status_counts={},
            quota_state=None,
        )
        assert "2" in result or "task-a" in result

    def test_includes_quota_percentage_from_state(self, fresh_state: Path) -> None:
        """CC usage percentage must appear when quota state is fresh."""
        state = read_quota_state(state_path=fresh_state)
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": True},
            status_counts={"ready-for-steward": 179, "executing": 1},
            quota_state=state,
        )
        assert "42" in result  # five_hour_pct from fresh_state fixture

    def test_includes_wos_queue_depth_from_status_counts(self) -> None:
        """WOS queue depth from status_counts must appear in the output."""
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": True},
            status_counts={"ready-for-steward": 179, "executing": 1},
            quota_state=None,
        )
        assert "179" in result

    def test_no_agents_message_when_empty(self) -> None:
        """When no agents are running, output should reflect that clearly."""
        result = format_status_message(
            active_sessions=[],
            wos_config={"execution_enabled": True},
            status_counts={},
            quota_state=None,
        )
        # Either "0" or "none" or "no agents" — any clear indicator is acceptable
        assert "0" in result or "none" in result.lower() or "no agent" in result.lower()


# ---------------------------------------------------------------------------
# handle_help — static text, no file reads
# ---------------------------------------------------------------------------


class TestHandleHelp:
    """handle_help returns a static command index."""

    def test_returns_string(self) -> None:
        assert isinstance(handle_help(), str)

    def test_returns_command_help_constant(self) -> None:
        """handle_help must return the COMMAND_HELP constant."""
        assert handle_help() == COMMAND_HELP

    def test_includes_todos_command(self) -> None:
        text = handle_help()
        assert "/todos" in text

    def test_includes_quota_command(self) -> None:
        """/quota must be listed in the help text after this PR."""
        text = handle_help()
        assert "/quota" in text

    def test_includes_status_command(self) -> None:
        """/status must be listed in the help text after this PR."""
        text = handle_help()
        assert "/status" in text

    def test_includes_help_command(self) -> None:
        text = handle_help()
        assert "/help" in text or "help" in text.lower()

    def test_includes_shop_command(self) -> None:
        text = handle_help()
        assert "/shop" in text

    def test_includes_re_review_command(self) -> None:
        text = handle_help()
        assert "/re-review" in text
