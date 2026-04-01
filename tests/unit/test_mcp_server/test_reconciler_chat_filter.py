"""
Unit tests for reconciler chat_id=0 filter (issue #462).

System/internal agents (chat_id=0) previously generated inbox notifications
even though the dispatcher cannot relay them to any user — every such message
was a no-op read that the dispatcher had to process and discard.

The fix extracts a pure helper (_is_internal_agent) and applies it uniformly
to both 'completed' and 'dead' outcomes in _enqueue_reconciler_notification,
replacing the previous partial filter that only covered 'dead'.

Strategy:
  - _is_internal_agent: pure predicate, tested exhaustively over all sentinel values
  - _enqueue_reconciler_notification: tested with mocked I/O to verify no inbox
    message is written and set_notified is NOT called for chat_id=0 agents
    (we do not want to accidentally consume the notified_at slot for internal agents)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import the helpers under test
# ---------------------------------------------------------------------------

from src.mcp.inbox_server import _is_internal_agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session(
    agent_id: str = "agent-1",
    chat_id: object = "42",
    status: str = "completed",
    description: str = "test task",
    notified_at: object = None,
    elapsed_seconds: int | None = 60,
    output_file: str | None = None,
) -> dict:
    return {
        "id": agent_id,
        "status": status,
        "chat_id": chat_id,
        "description": description,
        "task_id": agent_id,
        "source": "telegram",
        "elapsed_seconds": elapsed_seconds,
        "notified_at": notified_at,
        "output_file": output_file,
        "input_summary": None,
    }


# ---------------------------------------------------------------------------
# Tests: _is_internal_agent (pure predicate)
# ---------------------------------------------------------------------------


class TestIsInternalAgent:
    """_is_internal_agent correctly identifies sessions with no real user."""

    def test_integer_zero_is_internal(self):
        assert _is_internal_agent(_make_session(chat_id=0)) is True

    def test_string_zero_is_internal(self):
        assert _is_internal_agent(_make_session(chat_id="0")) is True

    def test_empty_string_is_internal(self):
        assert _is_internal_agent(_make_session(chat_id="")) is True

    def test_none_chat_id_is_internal(self):
        session = _make_session()
        session["chat_id"] = None
        assert _is_internal_agent(session) is True

    def test_string_none_is_internal(self):
        assert _is_internal_agent(_make_session(chat_id="None")) is True

    def test_whitespace_only_is_internal(self):
        assert _is_internal_agent(_make_session(chat_id="  ")) is True

    def test_real_user_chat_id_is_not_internal(self):
        assert _is_internal_agent(_make_session(chat_id="8075091586")) is False

    def test_numeric_real_chat_id_is_not_internal(self):
        assert _is_internal_agent(_make_session(chat_id=8075091586)) is False

    def test_small_positive_chat_id_is_not_internal(self):
        assert _is_internal_agent(_make_session(chat_id="1")) is False

    def test_missing_chat_id_key_is_internal(self):
        session = {"id": "x", "status": "completed"}
        assert _is_internal_agent(session) is True

    def test_pure_same_input_same_output(self):
        session = _make_session(chat_id="0")
        assert _is_internal_agent(session) == _is_internal_agent(session)

    def test_does_not_mutate_session(self):
        session = _make_session(chat_id="0")
        original = dict(session)
        _is_internal_agent(session)
        assert session == original


# ---------------------------------------------------------------------------
# Tests: _enqueue_reconciler_notification — internal agent filtering
# ---------------------------------------------------------------------------


class TestEnqueueReconcilerNotificationFilter:
    """Internal agents must never produce inbox messages for any outcome."""

    def _run(
        self,
        session: dict,
        outcome: str,
        tmp_path: Path,
    ) -> tuple[list[dict], MagicMock]:
        """Run _enqueue_reconciler_notification with patched I/O.

        Returns (written_messages, mock_session_store).
        """
        written: list[dict] = []

        def fake_atomic_write(path: Path, data: dict) -> None:
            written.append(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))

        mock_store = MagicMock()

        with (
            patch("src.mcp.inbox_server.INBOX_DIR", tmp_path),
            patch("src.mcp.inbox_server.atomic_write_json", side_effect=fake_atomic_write),
            patch("src.mcp.inbox_server._session_store", mock_store),
            patch("src.mcp.inbox_server._read_last_output", return_value=None),
        ):
            from src.mcp.inbox_server import _enqueue_reconciler_notification
            _enqueue_reconciler_notification(session, outcome)

        return written, mock_store

    # -- completed outcome --

    def test_completed_internal_agent_no_inbox_message(self, tmp_path):
        session = _make_session(chat_id="0", status="completed")
        written, _ = self._run(session, "completed", tmp_path)
        assert written == [], "chat_id=0 completed agent must not write to inbox"

    def test_completed_internal_agent_set_notified_not_called(self, tmp_path):
        """Internal agents are dropped before the notified_at write — no DB side effect."""
        session = _make_session(chat_id="0", status="completed")
        _, mock_store = self._run(session, "completed", tmp_path)
        mock_store.set_notified.assert_not_called()

    def test_completed_empty_chat_id_no_inbox_message(self, tmp_path):
        session = _make_session(chat_id="", status="completed")
        written, _ = self._run(session, "completed", tmp_path)
        assert written == []

    def test_completed_none_chat_id_no_inbox_message(self, tmp_path):
        session = _make_session(status="completed")
        session["chat_id"] = None
        written, _ = self._run(session, "completed", tmp_path)
        assert written == []

    # -- dead outcome --

    def test_dead_internal_agent_no_inbox_message(self, tmp_path):
        session = _make_session(chat_id="0", status="dead")
        written, _ = self._run(session, "dead", tmp_path)
        assert written == [], "chat_id=0 dead agent must not write to inbox"

    def test_dead_internal_agent_set_notified_not_called(self, tmp_path):
        session = _make_session(chat_id="0", status="dead")
        _, mock_store = self._run(session, "dead", tmp_path)
        mock_store.set_notified.assert_not_called()

    # -- real user — must still receive notifications --

    def test_completed_real_user_writes_inbox_message(self, tmp_path):
        session = _make_session(chat_id="8075091586", status="completed")
        written, _ = self._run(session, "completed", tmp_path)
        assert len(written) == 1
        assert written[0]["type"] == "subagent_result"
        assert written[0]["chat_id"] == "8075091586"

    def test_dead_real_user_writes_inbox_message(self, tmp_path):
        session = _make_session(chat_id="8075091586", status="dead")
        written, _ = self._run(session, "dead", tmp_path)
        assert len(written) == 1
        assert written[0]["type"] == "agent_failed"

    def test_completed_real_user_set_notified_called(self, tmp_path):
        session = _make_session(agent_id="my-agent", chat_id="8075091586", status="completed")
        _, mock_store = self._run(session, "completed", tmp_path)
        mock_store.set_notified.assert_called_once_with("my-agent")

    # -- idempotency guard still works for real users --

    def test_already_notified_real_user_no_duplicate(self, tmp_path):
        session = _make_session(chat_id="8075091586", status="completed", notified_at="2026-01-01T00:00:00")
        written, _ = self._run(session, "completed", tmp_path)
        assert written == [], "already-notified session must not write again"
