"""
Tests for the wos_pr_sweep_result dispatcher handler.

Spec:
  - ``wos_pr_sweep_result`` is registered in WOS_MESSAGE_TYPE_DISPATCH.
  - ``handle_wos_pr_sweep_result(msg)`` is a pure function returning
    action="send_reply" with the pre-formatted notification text.
  - ``chat_id`` is cast to int; falls back to LOBSTER_ADMIN_CHAT_ID env var,
    then to 0.
  - Missing ``text`` field falls back to the sentinel string
    "WOS PR sweep results (no detail available)".
  - Extra unknown fields do not raise.
  - ``route_wos_message`` dispatches ``wos_pr_sweep_result`` to
    ``handle_wos_pr_sweep_result`` via the fast-path block; on handler
    exception it returns action="send_reply" with error text.
  - The PR sweeper's ``_should_notify`` deduplication function honours a
    24-hour cooldown and treats corrupt records as no record.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.orchestration.dispatcher_handlers import (
    WOS_MESSAGE_TYPE_DISPATCH,
    handle_wos_pr_sweep_result,
    route_wos_message,
)

# ---------------------------------------------------------------------------
# Load wos-pr-sweeper.py via importlib (it is a script, not a package)
# ---------------------------------------------------------------------------

_sweeper_path = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "wos-pr-sweeper.py"
)
_spec = importlib.util.spec_from_file_location("wos_pr_sweeper", _sweeper_path)
_sweeper = importlib.util.module_from_spec(_spec)
import sys as _sys
_sys.modules["wos_pr_sweeper"] = _sweeper
_spec.loader.exec_module(_sweeper)

_should_notify = _sweeper._should_notify
NOTIFICATION_COOLDOWN_HOURS = _sweeper.NOTIFICATION_COOLDOWN_HOURS


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _make_sweep_msg(
    text: str = "2 stale open PRs found",
    chat_id: int = 12345,
    stale_open_count: int = 2,
    merged_pending_close_count: int = 0,
) -> dict:
    """Build a minimal wos_pr_sweep_result inbox message."""
    return {
        "type": "wos_pr_sweep_result",
        "text": text,
        "chat_id": chat_id,
        "data": {
            "stale_open_count": stale_open_count,
            "merged_pending_close_count": merged_pending_close_count,
        },
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestWosPrSweepResultRegistered:
    """wos_pr_sweep_result must be registered in the dispatch table."""

    def test_wos_pr_sweep_result_in_dispatch_table(self):
        assert "wos_pr_sweep_result" in WOS_MESSAGE_TYPE_DISPATCH, (
            "'wos_pr_sweep_result' must appear in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher's structural routing table can fire for it"
        )


# ---------------------------------------------------------------------------
# Stale open PR notification
# ---------------------------------------------------------------------------

class TestStaleOpenPRNotification:
    """Handler correctly surfaces stale-open-PR sweep results."""

    def test_returns_send_reply_action(self):
        msg = _make_sweep_msg(text="3 stale open PRs detected", stale_open_count=3)
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply"

    def test_text_matches_input(self):
        notification = "3 stale open PRs detected — review needed"
        msg = _make_sweep_msg(text=notification, stale_open_count=3)
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == notification

    def test_message_type_is_wos_pr_sweep_result(self):
        msg = _make_sweep_msg(stale_open_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert result["message_type"] == "wos_pr_sweep_result"

    def test_chat_id_cast_to_int(self):
        msg = _make_sweep_msg(chat_id=99999)
        result = handle_wos_pr_sweep_result(msg)
        assert result["chat_id"] == 99999
        assert isinstance(result["chat_id"], int)


# ---------------------------------------------------------------------------
# Merged PR / UoW not done notification
# ---------------------------------------------------------------------------

class TestMergedPRUoWNotDoneNotification:
    """Handler correctly surfaces merged-PR-with-pending-UoW sweep results."""

    def test_returns_send_reply_action(self):
        msg = _make_sweep_msg(
            text="1 merged PR has a non-done UoW",
            merged_pending_close_count=1,
        )
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply"

    def test_text_matches_input(self):
        notification = "PR #42 merged but UoW uow_20260501_abc123 is still open"
        msg = _make_sweep_msg(text=notification, merged_pending_close_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == notification

    def test_message_type_is_wos_pr_sweep_result(self):
        msg = _make_sweep_msg(merged_pending_close_count=2)
        result = handle_wos_pr_sweep_result(msg)
        assert result["message_type"] == "wos_pr_sweep_result"


# ---------------------------------------------------------------------------
# Fallback / graceful handling
# ---------------------------------------------------------------------------

class TestHandlerFallbackGracefulHandling:
    """Handler degrades gracefully when expected fields are absent."""

    def test_missing_text_uses_fallback_string(self):
        msg = {"type": "wos_pr_sweep_result", "chat_id": 1}
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == "WOS PR sweep results (no detail available)"

    def test_missing_chat_id_falls_back_to_zero(self, monkeypatch):
        monkeypatch.delenv("LOBSTER_ADMIN_CHAT_ID", raising=False)
        msg = {"type": "wos_pr_sweep_result", "text": "some results"}
        result = handle_wos_pr_sweep_result(msg)
        assert result["chat_id"] == 0

    def test_extra_unknown_fields_do_not_raise(self):
        msg = _make_sweep_msg()
        msg["unexpected_field"] = {"nested": True}
        msg["another_field"] = 42
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply"


# ---------------------------------------------------------------------------
# Deduplication cooldown (from wos-pr-sweeper.py)
# ---------------------------------------------------------------------------

class TestDeduplicationCooldown:
    """_should_notify honours NOTIFICATION_COOLDOWN_HOURS and handles edge cases."""

    def test_no_prior_record_returns_true(self):
        assert _should_notify("SiderealPress/lobster#99", {}) is True

    def test_notified_within_cooldown_returns_false(self):
        key = "SiderealPress/lobster#100"
        now_iso = datetime.now(timezone.utc).isoformat()
        state = {key: {"last_notified_at": now_iso}}
        assert _should_notify(key, state) is False

    def test_notified_beyond_cooldown_returns_true(self):
        key = "SiderealPress/lobster#101"
        old_iso = (
            datetime.now(timezone.utc) - timedelta(hours=NOTIFICATION_COOLDOWN_HOURS + 1)
        ).isoformat()
        state = {key: {"last_notified_at": old_iso}}
        assert _should_notify(key, state) is True

    def test_corrupt_last_notified_at_treated_as_no_record(self):
        key = "SiderealPress/lobster#102"
        state = {key: {"last_notified_at": "not-a-date"}}
        assert _should_notify(key, state) is True


# ---------------------------------------------------------------------------
# route_wos_message fast-path integration
# ---------------------------------------------------------------------------

class TestRouteWosMessageFastPath:
    """route_wos_message dispatches wos_pr_sweep_result via the fast-path block."""

    def test_well_formed_message_returns_send_reply(self):
        msg = _make_sweep_msg()
        result = route_wos_message(msg)
        assert result["action"] == "send_reply"

    def test_well_formed_message_returns_correct_message_type(self):
        msg = _make_sweep_msg()
        result = route_wos_message(msg)
        assert result["message_type"] == "wos_pr_sweep_result"

    def test_handler_exception_is_caught_returns_send_reply(self, monkeypatch):
        def _raise(msg):
            raise RuntimeError("simulated handler failure")

        monkeypatch.setattr(
            "src.orchestration.dispatcher_handlers.handle_wos_pr_sweep_result",
            _raise,
        )
        msg = _make_sweep_msg()
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "route_wos_message must catch handler exceptions and return "
            "action='send_reply' with error text rather than propagating"
        )
