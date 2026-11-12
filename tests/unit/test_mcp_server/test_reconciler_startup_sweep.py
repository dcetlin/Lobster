"""
Unit tests for reconciler startup sweep summary logic (issues #459, #464, #469).

The startup sweep previously wrote one inbox message per completed task, creating
inbox floods of 500+ messages after busy sessions (issue #459).  The fix groups
all unnotified sessions by chat_id and writes one summary per user (issue #469),
with a defense-in-depth batch cap (issue #464).

Strategy: test the pure helper functions extracted from inbox_server.py.
  - _build_startup_sweep_summary: pure message builder (no side effects)
  - _enqueue_startup_sweep_summaries: grouping + I/O, tested with mocked globals

Only the pure helpers and the grouping logic are tested here; the async
reconcile_agent_sessions loop is out of scope (requires full server mock).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import the pure helpers under test
# ---------------------------------------------------------------------------

from src.mcp.inbox_server import (
    _build_startup_sweep_summary,
    _STARTUP_SWEEP_BATCH_CAP,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_session(
    agent_id: str,
    status: str = "completed",
    chat_id: str = "42",
    description: str = "test task",
    task_id: str | None = None,
    source: str = "telegram",
    elapsed_seconds: int | None = 120,
) -> dict:
    return {
        "id": agent_id,
        "status": status,
        "chat_id": chat_id,
        "description": description,
        "task_id": task_id or agent_id,
        "source": source,
        "elapsed_seconds": elapsed_seconds,
        "notified_at": None,
    }


# ---------------------------------------------------------------------------
# Tests: _build_startup_sweep_summary (pure function)
# ---------------------------------------------------------------------------


class TestBuildStartupSweepSummary:
    """_build_startup_sweep_summary produces a correct message dict (no side effects)."""

    def test_returns_dict_with_required_keys(self):
        sessions = [_make_session("agent-1")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "id" in msg
        assert "type" in msg
        assert "chat_id" in msg
        assert "text" in msg
        assert "task_ids" in msg
        assert "completed_count" in msg
        assert "dead_count" in msg
        assert "sent_reply_to_user" in msg
        assert "timestamp" in msg

    def test_type_is_reconciler_sweep_summary(self):
        sessions = [_make_session("agent-1")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert msg["type"] == "reconciler_sweep_summary"

    def test_chat_id_matches_input(self):
        sessions = [_make_session("agent-1", chat_id="99")]
        msg = _build_startup_sweep_summary("99", sessions, NOW)
        assert msg["chat_id"] == "99"

    def test_sent_reply_to_user_is_false(self):
        sessions = [_make_session("agent-1")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert msg["sent_reply_to_user"] is False

    def test_counts_completed_and_dead(self):
        sessions = [
            _make_session("a1", status="completed"),
            _make_session("a2", status="completed"),
            _make_session("a3", status="dead"),
        ]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert msg["completed_count"] == 2
        assert msg["dead_count"] == 1

    def test_task_ids_collected(self):
        sessions = [
            _make_session("a1", task_id="task-alpha"),
            _make_session("a2", task_id="task-beta"),
        ]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "task-alpha" in msg["task_ids"]
        assert "task-beta" in msg["task_ids"]
        assert len(msg["task_ids"]) == 2

    def test_text_mentions_completed_count(self):
        sessions = [
            _make_session("a1", status="completed", description="My Task"),
            _make_session("a2", status="completed", description="Another Task"),
        ]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "2 task(s) completed" in msg["text"]

    def test_text_mentions_dead_count(self):
        sessions = [_make_session("a1", status="dead", description="Dead Task")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "1 task(s) disappeared" in msg["text"]

    def test_text_includes_description(self):
        sessions = [_make_session("a1", description="Oracle review for PR #42")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "Oracle review for PR #42" in msg["text"]

    def test_elapsed_minutes_in_text(self):
        sessions = [_make_session("a1", elapsed_seconds=300)]  # 5 minutes
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "5m" in msg["text"]

    def test_elapsed_none_handled_gracefully(self):
        sessions = [_make_session("a1", elapsed_seconds=None)]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        # Should not raise; 0m shown
        assert "0m" in msg["text"]

    def test_source_from_first_session(self):
        sessions = [
            _make_session("a1", source="slack"),
            _make_session("a2", source="telegram"),
        ]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert msg["source"] == "slack"

    def test_source_defaults_to_system_when_none(self):
        sessions = [_make_session("a1")]
        sessions[0]["source"] = None
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert msg["source"] == "system"

    def test_deterministic_for_same_input(self):
        """Pure function: same input → same output structure."""
        sessions = [_make_session("a1")]
        msg_a = _build_startup_sweep_summary("42", sessions, NOW)
        msg_b = _build_startup_sweep_summary("42", sessions, NOW)
        for key in ("type", "chat_id", "text", "completed_count", "dead_count", "source"):
            assert msg_a[key] == msg_b[key], f"Field {key!r} differs between calls"

    def test_empty_completed_no_completed_line(self):
        sessions = [_make_session("a1", status="dead")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "completed" not in msg["text"]

    def test_empty_dead_no_dead_line(self):
        sessions = [_make_session("a1", status="completed")]
        msg = _build_startup_sweep_summary("42", sessions, NOW)
        assert "disappeared" not in msg["text"]


# ---------------------------------------------------------------------------
# Tests: batch cap constant
# ---------------------------------------------------------------------------


class TestBatchCap:
    """The defense-in-depth cap is set to a reasonable value."""

    def test_cap_is_positive(self):
        assert _STARTUP_SWEEP_BATCH_CAP > 0

    def test_cap_is_at_most_100(self):
        # A cap larger than 100 would not provide meaningful flood protection
        assert _STARTUP_SWEEP_BATCH_CAP <= 100


# ---------------------------------------------------------------------------
# Tests: _enqueue_startup_sweep_summaries grouping logic (mocked side effects)
# ---------------------------------------------------------------------------


class TestEnqueueStartupSweepSummaries:
    """_enqueue_startup_sweep_summaries groups sessions by chat_id (1 message per user)."""

    def _run_with_mocks(self, sessions: list[dict], tmp_path: Path) -> tuple[list[dict], MagicMock]:
        """Run _enqueue_startup_sweep_summaries with patched I/O.

        Returns (written_messages, mock_store).
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
        ):
            from src.mcp.inbox_server import _enqueue_startup_sweep_summaries
            _enqueue_startup_sweep_summaries(sessions)

        return written, mock_store

    def test_two_sessions_same_chat_produces_one_message(self, tmp_path):
        sessions = [
            _make_session("a1", chat_id="42"),
            _make_session("a2", chat_id="42"),
        ]
        written, _ = self._run_with_mocks(sessions, tmp_path)
        assert len(written) == 1
        assert written[0]["type"] == "reconciler_sweep_summary"
        assert written[0]["completed_count"] == 2

    def test_two_sessions_different_chats_produces_two_messages(self, tmp_path):
        sessions = [
            _make_session("a1", chat_id="42"),
            _make_session("a2", chat_id="99"),
        ]
        written, _ = self._run_with_mocks(sessions, tmp_path)
        assert len(written) == 2
        chat_ids = {msg["chat_id"] for msg in written}
        assert "42" in chat_ids
        assert "99" in chat_ids

    def test_internal_sessions_excluded_from_inbox(self, tmp_path):
        sessions = [
            _make_session("internal-1", chat_id="0"),
            _make_session("internal-2", chat_id=""),
            _make_session("real-user", chat_id="42"),
        ]
        written, _ = self._run_with_mocks(sessions, tmp_path)
        # Only the real user gets a summary
        assert len(written) == 1
        assert written[0]["chat_id"] == "42"

    def test_internal_sessions_marked_notified_even_when_excluded(self, tmp_path):
        """Internal sessions must be marked notified to prevent accumulation across restarts."""
        sessions = [_make_session("internal-1", chat_id="0")]
        _, mock_store = self._run_with_mocks(sessions, tmp_path)
        mock_store.set_notified.assert_called_with("internal-1")

    def test_all_sessions_marked_notified_after_success(self, tmp_path):
        sessions = [
            _make_session("a1", chat_id="42"),
            _make_session("a2", chat_id="42"),
        ]
        _, mock_store = self._run_with_mocks(sessions, tmp_path)
        notified = {c.args[0] for c in mock_store.set_notified.call_args_list}
        assert "a1" in notified
        assert "a2" in notified

    def test_overflow_sessions_marked_notified_but_omitted_from_summary(self, tmp_path):
        """Sessions beyond the batch cap are marked notified but omitted from the summary text."""
        n = _STARTUP_SWEEP_BATCH_CAP + 5
        sessions = [_make_session(f"agent-{i}", chat_id="42") for i in range(n)]
        written, mock_store = self._run_with_mocks(sessions, tmp_path)

        # Exactly one summary message written
        assert len(written) == 1
        # Summary covers only the capped count
        assert written[0]["completed_count"] == _STARTUP_SWEEP_BATCH_CAP
        # All sessions (including overflow) marked notified
        assert mock_store.set_notified.call_count == n

    def test_empty_sessions_writes_no_messages(self, tmp_path):
        written, _ = self._run_with_mocks([], tmp_path)
        assert written == []

    def test_no_messages_when_all_internal(self, tmp_path):
        sessions = [
            _make_session("a1", chat_id="0"),
            _make_session("a2", chat_id=""),
        ]
        written, _ = self._run_with_mocks(sessions, tmp_path)
        assert written == []

    def test_n_tasks_one_chat_produces_one_message_not_n(self, tmp_path):
        """The core guarantee: N tasks for one user → 1 inbox message, not N.

        This is the root-cause fix for issue #469 — grouping sessions by chat_id
        makes the N-task → N-message flood structurally impossible.
        """
        n = 100
        sessions = [_make_session(f"agent-{i}", chat_id="42") for i in range(n)]
        written, _ = self._run_with_mocks(sessions, tmp_path)
        # With cap=20, the first 20 sessions go into the summary; the rest are notified
        # but not included. Either way, only 1 message is written to inbox.
        assert len(written) == 1


# ---------------------------------------------------------------------------
# Tests: internal sessions routing to task-outputs/ (issue #462)
# ---------------------------------------------------------------------------


class TestInternalSessionsTaskOutputsRouting:
    """Internal sessions (chat_id=0) should be routed to task-outputs/, not inbox."""

    def _run_with_mocks(
        self, sessions: list[dict], tmp_path: Path
    ) -> tuple[list[dict], list[dict], MagicMock]:
        """Run _enqueue_startup_sweep_summaries with patched I/O.

        Returns (inbox_messages, task_output_files, mock_store).
        """
        inbox_written: list[dict] = []
        task_outputs_written: list[dict] = []
        task_outputs_dir = tmp_path / "task-outputs"
        task_outputs_dir.mkdir(parents=True, exist_ok=True)

        def fake_atomic_write(path: Path, data: dict) -> None:
            inbox_written.append(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))

        mock_store = MagicMock()

        # Patch both inbox and task-outputs directories
        # Import the module to patch constants before they're used
        import src.mcp.inbox_server as inbox_mod

        original_task_outputs_dir = inbox_mod.TASK_OUTPUTS_DIR
        original_inbox_dir = inbox_mod.INBOX_DIR
        try:
            inbox_mod.TASK_OUTPUTS_DIR = task_outputs_dir
            inbox_mod.INBOX_DIR = tmp_path / "inbox"
            with (
                patch.object(inbox_mod, "atomic_write_json", side_effect=fake_atomic_write),
                patch.object(inbox_mod, "_session_store", mock_store),
            ):
                inbox_mod._enqueue_startup_sweep_summaries(sessions)
        finally:
            inbox_mod.TASK_OUTPUTS_DIR = original_task_outputs_dir
            inbox_mod.INBOX_DIR = original_inbox_dir

        # Read any task-output files written
        for f in task_outputs_dir.glob("*.json"):
            task_outputs_written.append(json.loads(f.read_text()))

        return inbox_written, task_outputs_written, mock_store

    def test_internal_sessions_routed_to_task_outputs(self, tmp_path):
        """Internal sessions (chat_id=0) are written to task-outputs/, not inbox."""
        sessions = [
            _make_session("internal-1", chat_id="0", description="Internal task A"),
            _make_session("internal-2", chat_id="0", description="Internal task B"),
        ]
        inbox, task_outputs, _ = self._run_with_mocks(sessions, tmp_path)
        # No inbox messages for internal sessions
        assert inbox == []
        # One task-output file for all internal sessions
        assert len(task_outputs) == 1
        assert task_outputs[0]["job_name"] == "reconciler-sweep-internal"
        assert task_outputs[0]["completed_count"] == 2
        assert "Internal task A" in task_outputs[0]["output"]
        assert "Internal task B" in task_outputs[0]["output"]

    def test_mixed_sessions_routes_correctly(self, tmp_path):
        """Internal sessions go to task-outputs/, real users to inbox."""
        sessions = [
            _make_session("internal-1", chat_id="0", description="Internal task"),
            _make_session("user-task", chat_id="42", description="User task"),
        ]
        inbox, task_outputs, _ = self._run_with_mocks(sessions, tmp_path)
        # One inbox message for the real user
        assert len(inbox) == 1
        assert inbox[0]["chat_id"] == "42"
        # One task-output file for the internal session
        assert len(task_outputs) == 1
        assert task_outputs[0]["completed_count"] == 1

    def test_internal_sessions_marked_notified(self, tmp_path):
        """Internal sessions are marked notified to prevent accumulation."""
        sessions = [_make_session("internal-1", chat_id="0")]
        _, _, mock_store = self._run_with_mocks(sessions, tmp_path)
        mock_store.set_notified.assert_called_with("internal-1")

    def test_dead_internal_sessions_counted_separately(self, tmp_path):
        """Dead internal sessions are counted as dead, not completed."""
        sessions = [
            _make_session("int-completed", chat_id="0", status="completed"),
            _make_session("int-dead", chat_id="0", status="dead"),
        ]
        _, task_outputs, _ = self._run_with_mocks(sessions, tmp_path)
        assert len(task_outputs) == 1
        assert task_outputs[0]["completed_count"] == 1
        assert task_outputs[0]["dead_count"] == 1
