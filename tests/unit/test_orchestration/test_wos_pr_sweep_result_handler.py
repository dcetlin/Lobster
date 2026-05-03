"""
Tests for the wos_pr_sweep_result dispatcher handler.

Spec:
  - New message type "wos_pr_sweep_result" is registered in WOS_MESSAGE_TYPE_DISPATCH.
  - handle_wos_pr_sweep_result(msg) is a pure function — no I/O.
  - Handler is a fast-path: returns action="send_reply" so the dispatcher surfaces
    pre-formatted sweep results directly to Dan without spawning a subagent.
  - Exempted from the spawn-gate (returns action="send_reply", not "spawn_subagent").
  - route_wos_message wraps the handler in try/except — on exception it returns
    action="send_reply" with an error alert (does not propagate the exception).
  - _should_notify() in wos-pr-sweeper.py enforces a NOTIFICATION_COOLDOWN_HOURS=24
    cooldown per PR key to prevent inbox flooding.

WOS-UoW: uow_20260502_2f0ca1
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.orchestration.dispatcher_handlers import (
    handle_wos_pr_sweep_result,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
)

# ---------------------------------------------------------------------------
# Load wos-pr-sweeper.py via importlib (script, not a package)
# ---------------------------------------------------------------------------

_sweeper_path = (
    Path(__file__).parent.parent.parent.parent / "scheduled-tasks" / "wos-pr-sweeper.py"
)
import sys as _sys

_spec = importlib.util.spec_from_file_location("wos_pr_sweeper", _sweeper_path)
_sweeper = importlib.util.module_from_spec(_spec)
_sys.modules["wos_pr_sweeper"] = _sweeper
_spec.loader.exec_module(_sweeper)

_should_notify = _sweeper._should_notify
NOTIFICATION_COOLDOWN_HOURS = _sweeper.NOTIFICATION_COOLDOWN_HOURS


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _make_sweep_msg(
    text: str = "2 stale open PRs found.",
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
        """wos_pr_sweep_result must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher's type-based routing cannot fire for
        these messages — sweep results would silently stall instead of reaching Dan.
        """
        assert "wos_pr_sweep_result" in WOS_MESSAGE_TYPE_DISPATCH, (
            "'wos_pr_sweep_result' must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally rather than via prose that is lost on compaction"
        )


# ---------------------------------------------------------------------------
# Stale open PR notifications
# ---------------------------------------------------------------------------

class TestStaleOpenPRNotification:
    """Handler returns a send_reply result for stale-open-PR messages."""

    def test_stale_open_returns_send_reply_action(self):
        """Handler returns action='send_reply' for a stale-open-PR message."""
        msg = _make_sweep_msg(text="PR #42 is stale (open >7 days).", stale_open_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply", (
            "wos_pr_sweep_result handler must return action='send_reply' — "
            "no subagent spawn is required; the sweeper text is pre-formatted for Dan"
        )

    def test_stale_open_text_matches_input(self):
        """Result text must match the input message's text field exactly."""
        notification = "1 stale open PR: SiderealPress/lobster#99 (open 10 days)"
        msg = _make_sweep_msg(text=notification, stale_open_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == notification, (
            "Handler must relay the pre-formatted sweeper text unchanged — "
            "the sweeper already formatted it for Dan"
        )

    def test_stale_open_message_type_in_result(self):
        """Result must include message_type='wos_pr_sweep_result'."""
        msg = _make_sweep_msg(stale_open_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert result["message_type"] == "wos_pr_sweep_result", (
            "message_type must be echoed so callers can confirm which handler fired"
        )

    def test_stale_open_chat_id_is_int(self):
        """Result chat_id must be cast to int regardless of input type."""
        msg = _make_sweep_msg(chat_id=99999, stale_open_count=1)
        result = handle_wos_pr_sweep_result(msg)
        assert isinstance(result["chat_id"], int)
        assert result["chat_id"] == 99999


# ---------------------------------------------------------------------------
# Merged PR / UoW not done notifications
# ---------------------------------------------------------------------------

class TestMergedPRUoWNotDoneNotification:
    """Handler returns a send_reply result for merged-pending-close messages."""

    def test_merged_pending_returns_send_reply_action(self):
        """Handler returns action='send_reply' for a merged-pending-close message."""
        msg = _make_sweep_msg(
            text="PR #55 merged but UoW uow_20260401_aabbcc is still 'complete'.",
            merged_pending_close_count=1,
            stale_open_count=0,
        )
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply"

    def test_merged_pending_text_matches_input(self):
        """Result text must match the input message's text field exactly."""
        notification = "1 merged PR with UoW not yet marked done."
        msg = _make_sweep_msg(
            text=notification,
            merged_pending_close_count=1,
            stale_open_count=0,
        )
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == notification

    def test_merged_pending_message_type_in_result(self):
        """Result must include message_type='wos_pr_sweep_result'."""
        msg = _make_sweep_msg(merged_pending_close_count=1, stale_open_count=0)
        result = handle_wos_pr_sweep_result(msg)
        assert result["message_type"] == "wos_pr_sweep_result"


# ---------------------------------------------------------------------------
# Fallback / graceful handling
# ---------------------------------------------------------------------------

class TestHandlerFallbackGracefulHandling:
    """Handler handles missing or extra fields without raising."""

    def test_missing_text_uses_fallback_string(self):
        """Missing 'text' field falls back to the spec-defined default string."""
        msg = {"type": "wos_pr_sweep_result", "chat_id": 12345}
        result = handle_wos_pr_sweep_result(msg)
        assert result["text"] == "WOS PR sweep results (no detail available)", (
            "Handler must use the spec fallback text when 'text' is absent — "
            "the dispatcher should not silently swallow an empty notification"
        )

    def test_missing_chat_id_falls_back_to_zero(self, monkeypatch):
        """Missing 'chat_id' falls back to 0 when LOBSTER_ADMIN_CHAT_ID is also unset."""
        monkeypatch.delenv("LOBSTER_ADMIN_CHAT_ID", raising=False)
        msg = {"type": "wos_pr_sweep_result", "text": "some sweep result"}
        result = handle_wos_pr_sweep_result(msg)
        assert result["chat_id"] == 0, (
            "chat_id must default to 0 when both msg['chat_id'] and "
            "LOBSTER_ADMIN_CHAT_ID are absent"
        )

    def test_extra_unknown_fields_do_not_raise(self):
        """Unknown fields in the message are ignored — handler does not raise."""
        msg = _make_sweep_msg()
        msg["unexpected_field"] = "some value"
        msg["another_extra"] = {"nested": True}
        result = handle_wos_pr_sweep_result(msg)
        assert result["action"] == "send_reply"


# ---------------------------------------------------------------------------
# Deduplication cooldown (wos-pr-sweeper._should_notify)
# ---------------------------------------------------------------------------

class TestDeduplicationCooldown:
    """_should_notify enforces a 24-hour per-PR notification cooldown."""

    def test_no_prior_record_should_notify(self):
        """A PR key with no prior state entry always passes the cooldown gate."""
        state: dict = {}
        assert _should_notify("SiderealPress/lobster#1", state) is True

    def test_recently_notified_should_not_notify(self):
        """A PR notified within the last 24 hours must NOT trigger another notification."""
        key = "SiderealPress/lobster#2"
        now_iso = datetime.now(timezone.utc).isoformat()
        state = {key: {"last_notified_at": now_iso}}
        assert _should_notify(key, state) is False, (
            f"A PR notified just now should not trigger again within "
            f"{NOTIFICATION_COOLDOWN_HOURS}h cooldown"
        )

    def test_expired_cooldown_should_notify(self):
        """A PR last notified >24 hours ago must re-trigger notification."""
        key = "SiderealPress/lobster#3"
        past_iso = (
            datetime.now(timezone.utc) - timedelta(hours=NOTIFICATION_COOLDOWN_HOURS + 1)
        ).isoformat()
        state = {key: {"last_notified_at": past_iso}}
        assert _should_notify(key, state) is True, (
            f"Cooldown expired (>{NOTIFICATION_COOLDOWN_HOURS}h ago) — "
            "notification should re-fire"
        )

    def test_corrupt_timestamp_treated_as_no_record(self):
        """A corrupt last_notified_at value is treated as no prior notification."""
        key = "SiderealPress/lobster#4"
        state = {key: {"last_notified_at": "not-a-date"}}
        assert _should_notify(key, state) is True, (
            "Corrupt timestamp must not block future notifications — "
            "fail-safe should default to notify"
        )


# ---------------------------------------------------------------------------
# route_wos_message fast-path integration
# ---------------------------------------------------------------------------

class TestRouteWosMessageFastPath:
    """route_wos_message dispatches wos_pr_sweep_result via the fast-path before the spawn-gate."""

    def test_route_returns_send_reply_action(self):
        """route_wos_message returns action='send_reply' for a well-formed sweep message."""
        msg = _make_sweep_msg()
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "wos_pr_sweep_result must be fast-pathed to send_reply — "
            "no subagent spawn is needed for pre-formatted sweep results"
        )

    def test_route_echoes_message_type(self):
        """result['message_type'] must be 'wos_pr_sweep_result' so callers confirm routing."""
        msg = _make_sweep_msg()
        result = route_wos_message(msg)
        assert result["message_type"] == "wos_pr_sweep_result"

    def test_route_handler_exception_returns_send_reply_alert(self):
        """If handle_wos_pr_sweep_result raises, route_wos_message returns a send_reply alert.

        The exception must NOT propagate — the dispatcher must always get an action back.
        """
        msg = _make_sweep_msg()
        with patch(
            "src.orchestration.dispatcher_handlers.handle_wos_pr_sweep_result",
            side_effect=RuntimeError("simulated handler failure"),
        ):
            result = route_wos_message(msg)

        assert result["action"] == "send_reply", (
            "route_wos_message must catch handler exceptions and return a send_reply alert — "
            "the dispatcher must not crash when the sweep handler raises"
        )
        assert "error" in result["text"].lower() or "raised" in result["text"].lower(), (
            "Error alert text must mention the failure so Dan knows sweep results were not delivered"
        )
