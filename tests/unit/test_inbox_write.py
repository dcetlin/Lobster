"""
Tests for src/utils/inbox_write.py

Verifies the behavior of the consolidated write_inbox_message() function
that replaces the copy-pasted implementations across 6 scheduled-task scripts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
        msg_id = write_inbox_message("daily-metrics", 1, "text", "2026-04-20T00:00:00Z")
        assert msg_id.startswith("daily-metrics_")

    def test_returns_msg_id_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        result = write_inbox_message("weekly-epistemic-retro", 99, "x", "2026-04-20T00:00:00Z")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_each_call_generates_unique_msg_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        id1 = write_inbox_message("job", 1, "a", "2026-04-20T00:00:00Z")
        id2 = write_inbox_message("job", 1, "b", "2026-04-20T00:00:00Z")
        assert id1 != id2

    def test_source_uses_lobster_default_source_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.setenv("LOBSTER_DEFAULT_SOURCE", "slack")
        write_inbox_message("job", 1, "msg", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        payload = json.loads(next(inbox.glob("*.json")).read_text())
        assert payload["source"] == "slack"

    def test_source_defaults_to_telegram_when_env_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        monkeypatch.delenv("LOBSTER_DEFAULT_SOURCE", raising=False)
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
        write_inbox_message("job", 1, "text", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        tmp_files = list(inbox.glob("*.tmp"))
        assert tmp_files == [], f"leftover .tmp files: {tmp_files}"

    def test_output_file_named_after_msg_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path))
        msg_id = write_inbox_message("surface-queue-delivery", 2, "t", "2026-04-20T00:00:00Z")
        inbox = tmp_path / "inbox"
        assert (inbox / f"{msg_id}.json").exists()
