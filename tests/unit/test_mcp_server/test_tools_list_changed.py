"""
Tests for notifications/tools/list_changed (agent-channel protocol v1.1,
refinement 4) — src/mcp/inbox_server.py.

Two independent pieces:

1. Capability advertisement: server.create_initialization_options() must
   default to advertising tools.listChanged=True. Before this change it
   silently defaulted to False (mcp SDK's own NotificationOptions()
   default), which tells every connecting client "I will never send you a
   list_changed notification" regardless of whether anything downstream
   ever sends one.

2. _notify_tools_list_changed_once(): the best-effort, once-per-session
   nudge sent from call_tool() on a session's first tool call — the
   furthest-upstream point reachable from this module without SDK changes.
   See the module-level comment above it in inbox_server.py for the scope
   limits this leaves (cannot broadcast to a session other than the one
   currently making a request).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server as srv  # noqa: E402
from mcp.server.lowlevel.server import NotificationOptions, request_ctx  # noqa: E402


class TestCapabilityAdvertisement:
    def test_default_call_advertises_tools_changed(self):
        """StreamableHTTPSessionManager (the transport this server actually
        runs under in production) calls create_initialization_options()
        with no arguments for every new HTTP session — the override must
        make that no-argument call advertise tools.listChanged=True."""
        opts = srv.server.create_initialization_options()
        assert opts.capabilities.tools is not None
        assert opts.capabilities.tools.listChanged is True

    def test_explicit_notification_options_are_respected(self):
        """The override only supplies a *default* — an explicit caller-
        supplied NotificationOptions must not be silently overridden."""
        opts = srv.server.create_initialization_options(NotificationOptions(tools_changed=False))
        assert opts.capabilities.tools.listChanged is False

    def test_explicit_tools_changed_true_still_works(self):
        opts = srv.server.create_initialization_options(NotificationOptions(tools_changed=True))
        assert opts.capabilities.tools.listChanged is True


class TestNotifyToolsListChangedOnce:
    @pytest.fixture(autouse=True)
    def _reset_notified_sessions(self):
        srv._tools_list_changed_notified_sessions.clear()
        yield
        srv._tools_list_changed_notified_sessions.clear()

    @pytest.mark.asyncio
    async def test_sends_once_for_a_session_first_call(self, monkeypatch):
        fake_session = MagicMock()
        fake_session.send_tool_list_changed = AsyncMock()
        fake_ctx = MagicMock()
        fake_ctx.session = fake_session

        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "session-A")
        token = request_ctx.set(fake_ctx)
        try:
            await srv._notify_tools_list_changed_once()
        finally:
            request_ctx.reset(token)

        fake_session.send_tool_list_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_call_for_same_session_is_a_noop(self, monkeypatch):
        fake_session = MagicMock()
        fake_session.send_tool_list_changed = AsyncMock()
        fake_ctx = MagicMock()
        fake_ctx.session = fake_session

        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "session-A")
        token = request_ctx.set(fake_ctx)
        try:
            await srv._notify_tools_list_changed_once()
            await srv._notify_tools_list_changed_once()
            await srv._notify_tools_list_changed_once()
        finally:
            request_ctx.reset(token)

        fake_session.send_tool_list_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_distinct_sessions_each_get_their_own_nudge(self, monkeypatch):
        fake_session_a = MagicMock()
        fake_session_a.send_tool_list_changed = AsyncMock()
        fake_session_b = MagicMock()
        fake_session_b.send_tool_list_changed = AsyncMock()

        ctx_a = MagicMock()
        ctx_a.session = fake_session_a
        ctx_b = MagicMock()
        ctx_b.session = fake_session_b

        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "session-A")
        token = request_ctx.set(ctx_a)
        try:
            await srv._notify_tools_list_changed_once()
        finally:
            request_ctx.reset(token)

        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "session-B")
        token = request_ctx.set(ctx_b)
        try:
            await srv._notify_tools_list_changed_once()
        finally:
            request_ctx.reset(token)

        fake_session_a.send_tool_list_changed.assert_awaited_once()
        fake_session_b.send_tool_list_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_request_context_is_a_silent_noop(self, monkeypatch):
        """Most unit tests in this suite call handle_* functions directly,
        bypassing call_tool() entirely — request_ctx is never set in that
        case (request_ctx.get() raises LookupError, contextvars' own
        behavior for an unset ContextVar). This must never raise, since it
        must never block a real tool call either."""
        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "stdio")

        await srv._notify_tools_list_changed_once()  # must not raise

        # Marked as notified anyway — never retried for this session, since
        # a permanently-unreachable session (e.g. stdio outside a request)
        # would otherwise retry on every single tool call forever.
        assert "stdio" in srv._tools_list_changed_notified_sessions

    @pytest.mark.asyncio
    async def test_send_failure_is_a_silent_noop(self, monkeypatch):
        """A transport-level failure sending the notification must never
        propagate up into a real tool call's response."""
        fake_session = MagicMock()
        fake_session.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("connection closed"))
        fake_ctx = MagicMock()
        fake_ctx.session = fake_session

        monkeypatch.setattr(srv, "_get_current_http_session_id", lambda: "session-A")
        token = request_ctx.set(fake_ctx)
        try:
            await srv._notify_tools_list_changed_once()  # must not raise
        finally:
            request_ctx.reset(token)

    @pytest.mark.asyncio
    async def test_notified_sessions_set_stays_bounded_past_cap(self, monkeypatch):
        """Regression test for bloom's finding on PR #1530: this daemon runs
        for days/weeks and serves one distinct HTTP session id per
        connection over its lifetime, so a bare, never-evicted set here was
        an unbounded memory leak. Drive well past
        _TOOLS_LIST_CHANGED_SESSIONS_MAX distinct sessions and confirm the
        set never grows past the cap — FIFO eviction of the oldest entries
        keeps it bounded regardless of how many sessions this process has
        ever seen."""
        fake_session = MagicMock()
        fake_session.send_tool_list_changed = AsyncMock()
        fake_ctx = MagicMock()
        fake_ctx.session = fake_session

        cap = srv._TOOLS_LIST_CHANGED_SESSIONS_MAX
        num_sessions = cap + 250  # comfortably past the cap

        for i in range(num_sessions):
            monkeypatch.setattr(srv, "_get_current_http_session_id", lambda i=i: f"session-{i}")
            token = request_ctx.set(fake_ctx)
            try:
                await srv._notify_tools_list_changed_once()
            finally:
                request_ctx.reset(token)
            # Never exceeds the cap at any point during the run, not just
            # at the end — a fix that only truncates after the fact could
            # still spike unbounded mid-run.
            assert len(srv._tools_list_changed_notified_sessions) <= cap

        assert len(srv._tools_list_changed_notified_sessions) == cap
        # FIFO, not arbitrary eviction: the earliest sessions are the ones
        # gone, the most recent `cap` sessions are still present.
        assert "session-0" not in srv._tools_list_changed_notified_sessions
        assert f"session-{num_sessions - 1}" in srv._tools_list_changed_notified_sessions
