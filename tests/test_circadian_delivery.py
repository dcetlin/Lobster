"""
Tests for circadian-aware message delivery.

Coverage:
- is_non_urgent classifies scheduled-job messages correctly
- is_non_urgent returns False for user replies and urgent-keyword messages
- queue_message appends valid JSONL entries
- flush_morning_queue delivers pending entries and marks them delivered
- flush_morning_queue is a no-op when the queue is empty or all delivered
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.delivery.circadian import (
    flush_morning_queue,
    is_morning_window,
    is_non_urgent,
    queue_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduled_msg(**overrides) -> dict:
    base = {
        "id": "daily-metrics_abc123",
        "type": "subagent_result",
        "task_id": "daily-metrics_abc123",
        "chat_id": 8075091586,
        "source": "telegram",
        "text": "Daily metrics — 2026-05-02\n\nGitHub: 3 open issues.",
        "status": "success",
        "sent_reply_to_user": False,
        "timestamp": "2026-05-02T20:00:00Z",
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# is_non_urgent
# ---------------------------------------------------------------------------

class TestIsNonUrgent:
    def test_scheduled_job_no_reply_to(self):
        msg = _make_scheduled_msg()
        assert is_non_urgent(msg) is True

    def test_urgent_user_reply(self):
        msg = _make_scheduled_msg(reply_to_message_id=99999)
        assert is_non_urgent(msg) is False

    def test_non_subagent_result_type(self):
        msg = _make_scheduled_msg(type="user_message")
        assert is_non_urgent(msg) is False

    def test_inbound_message_type(self):
        msg = _make_scheduled_msg(type="inbound")
        assert is_non_urgent(msg) is False

    def test_urgent_keyword_error_colon(self):
        msg = _make_scheduled_msg(text="Error: health check failed — 3 UoWs stale.")
        assert is_non_urgent(msg) is False

    def test_urgent_keyword_incident(self):
        msg = _make_scheduled_msg(text="System incident detected at 14:32 UTC.")
        assert is_non_urgent(msg) is False

    def test_urgent_keyword_starvation(self):
        msg = _make_scheduled_msg(text="Starvation detected: UoW uow_abc123 stuck for 72h.")
        assert is_non_urgent(msg) is False

    def test_daily_digest_is_non_urgent(self):
        msg = _make_scheduled_msg(
            task_id="nightly-consolidation_xyz",
            text="Nightly consolidation complete. 12 memories updated.",
        )
        assert is_non_urgent(msg) is True

    def test_philosophy_exploration_is_non_urgent(self):
        msg = _make_scheduled_msg(
            task_id="philosophy-explorer_xyz",
            text="Today's exploration: The Ship of Theseus and software identity.",
        )
        assert is_non_urgent(msg) is True


# ---------------------------------------------------------------------------
# is_morning_window
# ---------------------------------------------------------------------------

# Window bounds per spec: 06:00 <= hour < 10:00 (America/Los_Angeles)
WINDOW_OPEN_HOUR = 6    # first hour inside the window
WINDOW_LAST_HOUR = 9    # last hour inside the window
WINDOW_CLOSE_HOUR = 10  # first hour outside (exclusive upper bound)
BEFORE_WINDOW_HOUR = 5  # hour before the window opens


def _make_now_fn(hour: int):
    """Return a _now_fn callable that yields a fixed datetime at the given hour."""
    from zoneinfo import ZoneInfo
    _PACIFIC = ZoneInfo("America/Los_Angeles")
    fixed = datetime(2026, 5, 2, hour, 0, 0, tzinfo=_PACIFIC)
    return lambda: fixed


class TestIsMorningWindow:
    def test_window_open_hour_is_inside(self):
        """hour=6 is the first hour of the window — must return True."""
        assert is_morning_window(_now_fn=_make_now_fn(WINDOW_OPEN_HOUR)) is True

    def test_window_last_hour_is_inside(self):
        """hour=9 is the last valid hour — must return True."""
        assert is_morning_window(_now_fn=_make_now_fn(WINDOW_LAST_HOUR)) is True

    def test_window_close_hour_is_outside(self):
        """hour=10 is just past the window (exclusive upper bound) — must return False."""
        assert is_morning_window(_now_fn=_make_now_fn(WINDOW_CLOSE_HOUR)) is False

    def test_before_window_is_outside(self):
        """hour=5 is before the window opens — must return False."""
        assert is_morning_window(_now_fn=_make_now_fn(BEFORE_WINDOW_HOUR)) is False


# ---------------------------------------------------------------------------
# queue_message
# ---------------------------------------------------------------------------

class TestQueueMessage:
    def test_appends_entry(self, tmp_path, monkeypatch):
        queue_file = tmp_path / "data" / "pending-deliveries.jsonl"
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        queue_message(8075091586, "Hello, morning!", source="daily-metrics")

        lines = queue_file.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["chat_id"] == 8075091586
        assert entry["text"] == "Hello, morning!"
        assert entry["source"] == "daily-metrics"
        assert entry["source_type"] == "scheduled_job"
        assert entry["delivered"] is False
        assert "queued_at" in entry

    def test_appends_multiple(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        queue_message(111, "Message A", source="job-a")
        queue_message(222, "Message B", source="job-b")

        lines = (tmp_path / "data" / "pending-deliveries.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["chat_id"] == 111
        assert json.loads(lines[1])["chat_id"] == 222


# ---------------------------------------------------------------------------
# flush_morning_queue
# ---------------------------------------------------------------------------

class TestFlushMorningQueue:
    def test_delivers_pending_and_marks_delivered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        queue_file = tmp_path / "data" / "pending-deliveries.jsonl"

        entries = [
            {"queued_at": "2026-05-02T08:00:00+00:00", "chat_id": 111, "text": "Msg A",
             "source": "job-a", "source_type": "scheduled_job", "delivered": False},
            {"queued_at": "2026-05-02T09:00:00+00:00", "chat_id": 222, "text": "Msg B",
             "source": "job-b", "source_type": "scheduled_job", "delivered": False},
        ]
        queue_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        calls = []
        def mock_send(chat_id, text):
            calls.append((chat_id, text))

        count = flush_morning_queue(mock_send)

        assert count == 2
        assert calls == [(111, "Msg A"), (222, "Msg B")]

        written = [json.loads(l) for l in queue_file.read_text().splitlines() if l.strip()]
        assert all(e["delivered"] is True for e in written)

    def test_skips_already_delivered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        queue_file = tmp_path / "data" / "pending-deliveries.jsonl"

        entries = [
            {"queued_at": "2026-05-02T08:00:00+00:00", "chat_id": 111, "text": "Old",
             "source": "job-a", "source_type": "scheduled_job", "delivered": True},
            {"queued_at": "2026-05-02T09:00:00+00:00", "chat_id": 222, "text": "New",
             "source": "job-b", "source_type": "scheduled_job", "delivered": False},
        ]
        queue_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        calls = []
        count = flush_morning_queue(lambda c, t: calls.append((c, t)))

        assert count == 1
        assert calls == [(222, "New")]

    def test_empty_queue_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        count = flush_morning_queue(lambda c, t: None)
        assert count == 0

    def test_absent_queue_file_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        # Do not create the file
        count = flush_morning_queue(lambda c, t: None)
        assert count == 0

    def test_failed_send_leaves_entry_undelivered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        queue_file = tmp_path / "data" / "pending-deliveries.jsonl"

        entry = {"queued_at": "2026-05-02T08:00:00+00:00", "chat_id": 111, "text": "X",
                 "source": "job-a", "source_type": "scheduled_job", "delivered": False}
        queue_file.write_text(json.dumps(entry) + "\n")

        def boom(chat_id, text):
            raise RuntimeError("network error")

        count = flush_morning_queue(boom)

        assert count == 0
        written = json.loads(queue_file.read_text().splitlines()[0])
        assert written["delivered"] is False
