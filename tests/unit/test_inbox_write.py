"""
Tests for src/utils/inbox_write.py

Verifies the behavior of the consolidated write_inbox_message() function
that replaces the copy-pasted implementations across 6 scheduled-task scripts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.inbox_write import _inbox_dir, _task_outputs_dir, write_inbox_message


# ---------------------------------------------------------------------------
# _inbox_dir / _task_outputs_dir
# ---------------------------------------------------------------------------

class TestDirectoryHelpers:
    def test_inbox_dir_uses_lobster_messages_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        result = _inbox_dir()
        assert result == tmp_path / "inbox"
        assert result.is_dir()

    def test_task_outputs_dir_uses_lobster_messages_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        result = _task_outputs_dir()
        assert result == tmp_path / "task-outputs"
        assert result.is_dir()

    def test_inbox_dir_falls_back_to_home_messages(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOBSTER_MESSAGES", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _inbox_dir()
        assert result == tmp_path / "messages" / "inbox"

    def test_directories_are_created_if_absent(self, tmp_path, monkeypatch):
        messages_root = tmp_path / "new_messages_dir"
        monkeypatch.setenv("LOBSTER_MESSAGES", str(messages_root))
        assert not messages_root.exists()
        _inbox_dir()
        assert (messages_root / "inbox").is_dir()


# ---------------------------------------------------------------------------
# write_inbox_message — schema correctness
# ---------------------------------------------------------------------------

class TestWriteInboxMessageSchema:
    def test_writes_valid_json_with_correct_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.setenv("LOBSTER_DEFAULT_SOURCE", "telegram")
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            msg_id = write_inbox_message(
                job_name="test-job",
                chat_id=12345,
                text="hello world",
                timestamp="2026-04-20T01:00:00+00:00",
            )
        inbox = tmp_path / "inbox"
        written = list(inbox.glob("*.json"))
        assert len(written) == 1
        payload = json.loads(written[0].read_text())
        assert payload["type"] == "subagent_result"
        assert payload["chat_id"] == 12345
        assert payload["text"] == "hello world"
        assert payload["status"] == "success"
        assert payload["sent_reply_to_user"] is False
        assert payload["timestamp"] == "2026-04-20T01:00:00+00:00"
        assert payload["id"] == msg_id
        assert payload["task_id"] == msg_id

    def test_msg_id_prefixed_with_job_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            msg_id = write_inbox_message("daily-metrics", 1, "text", "2026-04-20T00:00:00Z")
        assert msg_id.startswith("daily-metrics_")

    def test_returns_msg_id_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            result = write_inbox_message("weekly-epistemic-retro", 99, "x", "2026-04-20T00:00:00Z")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_each_call_generates_unique_msg_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            id1 = write_inbox_message("job", 1, "a", "2026-04-20T00:00:00Z")
            id2 = write_inbox_message("job", 1, "b", "2026-04-20T00:00:00Z")
        assert id1 != id2

    def test_source_uses_lobster_default_source_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.setenv("LOBSTER_DEFAULT_SOURCE", "slack")
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            write_inbox_message("job", 1, "msg", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        payload = json.loads(next(inbox.glob("*.json")).read_text())
        assert payload["source"] == "slack"

    def test_source_defaults_to_telegram_when_env_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.delenv("LOBSTER_DEFAULT_SOURCE", raising=False)
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            write_inbox_message("job", 1, "msg", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        payload = json.loads(next(inbox.glob("*.json")).read_text())
        assert payload["source"] == "telegram"


# ---------------------------------------------------------------------------
# write_inbox_message — atomic write (no .tmp files left behind)
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_tmp_file_left_after_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            write_inbox_message("job", 1, "text", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        tmp_files = list(inbox.glob("*.tmp"))
        assert tmp_files == [], f"leftover .tmp files: {tmp_files}"

    def test_output_file_named_after_msg_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            msg_id = write_inbox_message("surface-queue-delivery", 2, "t", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        assert (inbox / f"{msg_id}.json").exists()


# ---------------------------------------------------------------------------
# write_inbox_message — circadian deferral path
# ---------------------------------------------------------------------------

class TestCircadianDeferral:
    """
    Verifies that non-urgent messages sent outside the morning window are
    queued in pending-deliveries.jsonl instead of being written to the inbox.
    """

    def test_non_urgent_outside_morning_window_queued_not_inboxed(self, tmp_path, monkeypatch):
        """When is_non_urgent() is True and is_morning_window() is False, message goes to queue."""
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=False):
            msg_id = write_inbox_message("nightly-metrics", 42, "report text", "2026-04-20T03:00:00Z")
        # Inbox must be empty — message was deferred
        inbox = tmp_path / "inbox"
        written = list(inbox.glob("*.json")) if inbox.exists() else []
        assert written == [], f"Expected deferred message not in inbox, but found: {written}"
        # Queue file must exist with the entry
        queue_path = tmp_path / "data" / "pending-deliveries.jsonl"
        assert queue_path.exists(), "Expected pending-deliveries.jsonl to be created"
        entries = [json.loads(line) for line in queue_path.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["chat_id"] == 42
        assert entries[0]["text"] == "report text"
        assert entries[0]["delivered"] is False
        # msg_id is still returned to the caller
        assert isinstance(msg_id, str)
        assert msg_id.startswith("nightly-metrics_")

    def test_non_urgent_inside_morning_window_goes_to_inbox(self, tmp_path, monkeypatch):
        """When is_non_urgent() is True and is_morning_window() is True, message is written to inbox."""
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=True):
            msg_id = write_inbox_message("morning-job", 42, "morning report", "2026-04-20T14:00:00Z")
        inbox = tmp_path / "inbox"
        written = list(inbox.glob("*.json"))
        assert len(written) == 1
        payload = json.loads(written[0].read_text())
        assert payload["chat_id"] == 42
        assert payload["text"] == "morning report"

    def test_urgent_message_bypasses_deferral(self, tmp_path, monkeypatch):
        """Urgent messages (containing alert keywords) are written to inbox even outside morning window."""
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        with patch("src.delivery.circadian.is_morning_window", return_value=False):
            msg_id = write_inbox_message(
                "health-check", 42,
                "health check failed: disk usage at 95%",
                "2026-04-20T03:00:00Z",
            )
        inbox = tmp_path / "inbox"
        written = list(inbox.glob("*.json"))
        assert len(written) == 1, "Urgent message must bypass deferral and go to inbox immediately"
        payload = json.loads(written[0].read_text())
        assert "health check failed" in payload["text"]
