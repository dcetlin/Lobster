"""
Unit tests for src/daemons/wos_execute_router.py

Tests are derived from the spec in Issue #940 and the approved design doc
(~/lobster-workspace/workstreams/wos/design/wos-execute-router-daemon.md).

Coverage:
- execution_enabled=false gate skips routing
- MAX_AGENTS_GATE defers when active agent count >= threshold
- Messages without type=wos_execute are ignored
- A valid wos_execute message is claimed, dispatched, and marked processed
- A send_reply decision (spawn-gate alert) triggers an inbox alert but does
  not mark the message failed
- A dispatch exception marks the message failed and writes an alert
- route_wos_message raising an exception marks failed and writes an alert
- Claim race condition (file already gone) is handled gracefully
- run_poll_cycle returns 0 when gated out
- run_poll_cycle returns the count of wos_execute messages found
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Module loading helper
# ---------------------------------------------------------------------------

def _get_router_module():
    """Import wos_execute_router with patched heavy dependencies."""
    # The module imports orchestration and agents at module level — patch them
    # before the import so tests stay fast and hermetic.
    import importlib

    mocks = {
        "orchestration": MagicMock(),
        "orchestration.dispatcher_handlers": MagicMock(),
        "orchestration.executor": MagicMock(),
        "agents": MagicMock(),
        "agents.session_store": MagicMock(),
        "utils": MagicMock(),
        "utils.inbox_write": MagicMock(),
    }
    with patch.dict("sys.modules", mocks):
        # Force reimport each test so mocks are isolated
        mod_name = "src.daemons.wos_execute_router"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        # Add repo root to path if needed
        repo_root = Path(__file__).resolve().parent.parent
        src_root = repo_root / "src"
        for p in [str(repo_root), str(src_root)]:
            if p not in sys.path:
                sys.path.insert(0, p)

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wos_execute_router",
            repo_root / "src" / "daemons" / "wos_execute_router.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # Inject mocked top-level names the module uses after import
        mod.route_wos_message = MagicMock()
        mod._dispatch_via_claude_p = MagicMock()
        mod.get_active_sessions = MagicMock()
        mod.write_inbox_message = MagicMock()
        mod.read_wos_config = MagicMock()
        spec.loader.exec_module(mod)
        return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def router(tmp_path):
    """Return the router module with all external I/O mocked."""
    mod = _get_router_module()

    # Override directory constants to point at tmp_path
    mod.INBOX_DIR = tmp_path / "inbox"
    mod.PROCESSING_DIR = tmp_path / "processing"
    mod.PROCESSED_DIR = tmp_path / "processed"
    mod.FAILED_DIR = tmp_path / "failed"
    mod.MAX_AGENTS_GATE = 8

    # Default: execution enabled, 0 active agents
    mod.read_wos_config.return_value = {"execution_enabled": True}
    mod.get_active_sessions.return_value = []

    return mod


def _write_msg(directory: Path, msg: dict) -> Path:
    """Write a message JSON file to directory, creating it if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{msg['id']}.json"
    path.write_text(json.dumps(msg), encoding="utf-8")
    return path


def _make_wos_execute_msg(uow_id: str = "uow-abc123") -> dict:
    return {
        "id": f"msg-{uow_id}",
        "type": "wos_execute",
        "uow_id": uow_id,
        "instructions": "do something",
        "output_ref": "/tmp/out.json",
        "agent_type": "functional-engineer",
        "source": "system",
        "chat_id": "0",
        "timestamp": "2026-04-25T00:00:00+00:00",
    }


def _make_text_msg() -> dict:
    return {
        "id": "msg-text-001",
        "type": "text",
        "text": "hello",
        "source": "telegram",
        "chat_id": "8075091586",
        "timestamp": "2026-04-25T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Tests: execution_enabled gate
# ---------------------------------------------------------------------------

class TestExecutionEnabledGate:
    """run_poll_cycle skips all routing when execution_enabled is False."""

    def test_skips_routing_when_disabled(self, router, tmp_path):
        """Gate=false: no messages are claimed even if wos_execute messages exist."""
        router.read_wos_config.return_value = {"execution_enabled": False}
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        result = router.run_poll_cycle()

        assert result == 0
        # Message must still be in inbox (not claimed)
        assert (router.INBOX_DIR / f"{msg['id']}.json").exists()

    def test_returns_zero_when_disabled(self, router):
        """run_poll_cycle returns 0 when execution is disabled."""
        router.read_wos_config.return_value = {"execution_enabled": False}
        assert router.run_poll_cycle() == 0

    def test_routes_when_enabled(self, router, tmp_path):
        """Gate=true: wos_execute messages are processed."""
        router.read_wos_config.return_value = {"execution_enabled": True}
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run this",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id-1"

        result = router.run_poll_cycle()
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: MAX_AGENTS_GATE
# ---------------------------------------------------------------------------

class TestMaxAgentsGate:
    """run_poll_cycle defers when active agent count >= MAX_AGENTS_GATE."""

    def test_defers_when_at_threshold(self, router):
        """Exactly MAX_AGENTS_GATE agents active: skip routing."""
        router.get_active_sessions.return_value = [{}] * router.MAX_AGENTS_GATE
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        result = router.run_poll_cycle()

        assert result == 0
        # Message still in inbox — not claimed
        assert (router.INBOX_DIR / f"{msg['id']}.json").exists()

    def test_defers_when_above_threshold(self, router):
        """More than MAX_AGENTS_GATE agents: skip routing."""
        router.get_active_sessions.return_value = [{}] * (router.MAX_AGENTS_GATE + 2)

        result = router.run_poll_cycle()
        assert result == 0

    def test_routes_when_below_threshold(self, router):
        """Fewer than MAX_AGENTS_GATE agents: proceed with routing."""
        router.get_active_sessions.return_value = [{}] * (router.MAX_AGENTS_GATE - 1)
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id-2"

        result = router.run_poll_cycle()
        assert result == 1

    def test_returns_zero_when_at_threshold(self, router):
        router.get_active_sessions.return_value = [{}] * router.MAX_AGENTS_GATE
        assert router.run_poll_cycle() == 0


# ---------------------------------------------------------------------------
# Tests: message filtering
# ---------------------------------------------------------------------------

class TestMessageFiltering:
    """Only type=wos_execute messages are routed; others are left in inbox."""

    def test_ignores_text_messages(self, router):
        """Text messages are not claimed or routed."""
        msg = _make_text_msg()
        _write_msg(router.INBOX_DIR, msg)

        result = router.run_poll_cycle()

        assert result == 0
        assert (router.INBOX_DIR / f"{msg['id']}.json").exists()
        router.route_wos_message.assert_not_called()

    def test_routes_wos_execute_not_text(self, router):
        """Mixed inbox: only the wos_execute message is routed."""
        wos_msg = _make_wos_execute_msg()
        text_msg = _make_text_msg()
        _write_msg(router.INBOX_DIR, wos_msg)
        _write_msg(router.INBOX_DIR, text_msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{wos_msg['uow_id']}",
            "prompt": "run",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id-3"

        result = router.run_poll_cycle()

        # Only the wos_execute message counts
        assert result == 1
        # Text message stays in inbox
        assert (router.INBOX_DIR / f"{text_msg['id']}.json").exists()

    def test_empty_inbox_returns_zero(self, router):
        """Empty inbox returns 0 with no errors."""
        router.INBOX_DIR.mkdir(parents=True, exist_ok=True)
        assert router.run_poll_cycle() == 0


# ---------------------------------------------------------------------------
# Tests: happy-path routing (spawn_subagent)
# ---------------------------------------------------------------------------

class TestHappyPathRouting:
    """Valid wos_execute messages are claimed, dispatched, and marked processed."""

    def test_message_moved_to_processed_after_dispatch(self, router):
        """After successful dispatch, message is in processed/ not inbox/."""
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run this",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id"

        router.run_poll_cycle()

        # Message in processed/, not in inbox/ or failed/
        assert not (router.INBOX_DIR / f"{msg['id']}.json").exists()
        assert (router.PROCESSED_DIR / f"{msg['id']}.json").exists()

    def test_dispatch_called_with_stripped_uow_id(self, router):
        """_dispatch_via_claude_p receives uow_id with 'wos-' prefix stripped."""
        uow_id = "abc-456"
        msg = _make_wos_execute_msg(uow_id=uow_id)
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{uow_id}",
            "prompt": "run this",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id"

        router.run_poll_cycle()

        # uow_id passed to _dispatch_via_claude_p must NOT have "wos-" prefix
        call_kwargs = router._dispatch_via_claude_p.call_args
        assert call_kwargs is not None
        # Accept either positional or keyword argument for uow_id
        kwargs = call_kwargs.kwargs
        args = call_kwargs.args
        passed_uow_id = kwargs.get("uow_id") or (args[1] if len(args) > 1 else None)
        assert passed_uow_id == uow_id, (
            f"Expected uow_id={uow_id!r} but got {passed_uow_id!r}. "
            "The 'wos-' prefix must be stripped before passing to _dispatch_via_claude_p."
        )

    def test_route_wos_message_called_with_message(self, router):
        """route_wos_message is called with the original message dict."""
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.return_value = "run-id"

        router.run_poll_cycle()

        router.route_wos_message.assert_called_once()
        called_msg = router.route_wos_message.call_args[0][0]
        assert called_msg["id"] == msg["id"]
        assert called_msg["type"] == "wos_execute"


# ---------------------------------------------------------------------------
# Tests: send_reply action (spawn-gate alert)
# ---------------------------------------------------------------------------

class TestSendReplyAlert:
    """send_reply decision from route_wos_message triggers an inbox alert."""

    def test_alert_written_on_send_reply_action(self, router):
        """write_inbox_message is called when action=send_reply."""
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "send_reply",
            "text": "spawn-gate alert: handler raised an error",
            "message_type": "wos_execute",
        }

        router.run_poll_cycle()

        router.write_inbox_message.assert_called_once()

    def test_message_marked_processed_after_send_reply(self, router):
        """After a send_reply alert, message moves to processed/ (not failed/)."""
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "send_reply",
            "text": "alert text",
            "message_type": "wos_execute",
        }

        router.run_poll_cycle()

        assert (router.PROCESSED_DIR / f"{msg['id']}.json").exists()
        assert not (router.FAILED_DIR / f"{msg['id']}.json").exists()

    def test_dispatch_not_called_on_send_reply(self, router):
        """_dispatch_via_claude_p is not called when action=send_reply."""
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "send_reply",
            "text": "alert",
            "message_type": "wos_execute",
        }

        router.run_poll_cycle()

        router._dispatch_via_claude_p.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: dispatch failure
# ---------------------------------------------------------------------------

class TestDispatchFailure:
    """_dispatch_via_claude_p raising moves message to failed/ and writes alert."""

    def test_message_moved_to_failed_on_dispatch_error(self, router):
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.side_effect = RuntimeError("subprocess died")

        router.run_poll_cycle()

        assert (router.FAILED_DIR / f"{msg['id']}.json").exists()
        assert not (router.PROCESSED_DIR / f"{msg['id']}.json").exists()

    def test_alert_written_on_dispatch_error(self, router):
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.return_value = {
            "action": "spawn_subagent",
            "task_id": f"wos-{msg['uow_id']}",
            "prompt": "run",
            "agent_type": "functional-engineer",
            "message_type": "wos_execute",
        }
        router._dispatch_via_claude_p.side_effect = RuntimeError("subprocess died")

        router.run_poll_cycle()

        router.write_inbox_message.assert_called_once()

    def test_other_messages_continue_after_failure(self, router):
        """A dispatch failure for one UoW does not prevent routing of the next."""
        msg1 = _make_wos_execute_msg("uow-fail-001")
        msg2 = _make_wos_execute_msg("uow-ok-002")
        _write_msg(router.INBOX_DIR, msg1)
        _write_msg(router.INBOX_DIR, msg2)

        def fake_dispatch(instructions: str, uow_id: str) -> str:
            if "fail" in uow_id:
                raise RuntimeError("dispatch failed")
            return "run-ok"

        router.route_wos_message.side_effect = [
            {
                "action": "spawn_subagent",
                "task_id": f"wos-{msg1['uow_id']}",
                "prompt": "run",
                "agent_type": "functional-engineer",
                "message_type": "wos_execute",
            },
            {
                "action": "spawn_subagent",
                "task_id": f"wos-{msg2['uow_id']}",
                "prompt": "run",
                "agent_type": "functional-engineer",
                "message_type": "wos_execute",
            },
        ]
        router._dispatch_via_claude_p.side_effect = fake_dispatch

        router.run_poll_cycle()

        # msg1 failed, msg2 succeeded
        assert (router.FAILED_DIR / f"{msg1['id']}.json").exists()
        assert (router.PROCESSED_DIR / f"{msg2['id']}.json").exists()


# ---------------------------------------------------------------------------
# Tests: route_wos_message exception
# ---------------------------------------------------------------------------

class TestRouteWosMessageException:
    """route_wos_message raising moves message to failed/ and writes alert."""

    def test_message_moved_to_failed_on_route_exception(self, router):
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.side_effect = ValueError("bad message format")

        router.run_poll_cycle()

        assert (router.FAILED_DIR / f"{msg['id']}.json").exists()

    def test_alert_written_on_route_exception(self, router):
        msg = _make_wos_execute_msg()
        _write_msg(router.INBOX_DIR, msg)

        router.route_wos_message.side_effect = ValueError("bad message format")

        router.run_poll_cycle()

        router.write_inbox_message.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: claim race condition
# ---------------------------------------------------------------------------

class TestClaimRaceCondition:
    """If message disappears from inbox before claim, routing is skipped silently."""

    def test_already_claimed_message_skipped_gracefully(self, router):
        """Message removed from inbox before claim — no error, no crash."""
        msg = _make_wos_execute_msg()
        # Deliberately do NOT write the file — simulates a race condition where
        # another process claimed it between check_inbox and our claim attempt
        msg["_filepath"] = str(router.INBOX_DIR / f"{msg['id']}.json")

        # Inject directly into the read result by monkeypatching _read_inbox_messages
        with patch.object(router, "_read_inbox_messages", return_value=[msg]):
            result = router.run_poll_cycle()

        # Should not crash; dispatch never called
        router._dispatch_via_claude_p.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _filter_wos_execute (pure function)
# ---------------------------------------------------------------------------

class TestFilterWosExecute:
    """_filter_wos_execute is a pure filter — no side effects."""

    def test_returns_only_wos_execute_messages(self):
        mod = _get_router_module()
        msgs = [
            {"type": "wos_execute", "id": "a"},
            {"type": "text", "id": "b"},
            {"type": "subagent_notification", "id": "c"},
            {"type": "wos_execute", "id": "d"},
        ]
        result = mod._filter_wos_execute(msgs)
        assert [m["id"] for m in result] == ["a", "d"]

    def test_returns_empty_for_no_matches(self):
        mod = _get_router_module()
        msgs = [{"type": "text"}, {"type": "callback"}]
        assert mod._filter_wos_execute(msgs) == []

    def test_returns_empty_for_empty_input(self):
        mod = _get_router_module()
        assert mod._filter_wos_execute([]) == []

    def test_ignores_case_variation(self):
        """Type matching is exact — 'WOS_EXECUTE' is not a match."""
        mod = _get_router_module()
        msgs = [{"type": "WOS_EXECUTE"}, {"type": "wos-execute"}]
        assert mod._filter_wos_execute(msgs) == []
