"""
Tests for src/mcp/agent_channel.py — the extracted agent-channel (source=
"local-claude") protocol mechanics: envelope construction, the single-shot
reply-slot write, the ack write, and the agent_channel.* audit emits.

This module takes its dependencies (paths, the event emitter) as explicit
parameters, so it is testable directly without booting the MCP server or
patching inbox_server.py globals — see the module docstring for why.
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure src/mcp is on sys.path so `agent_channel` resolves the same way
# inbox_server.py imports it (sibling import), and src/ so `src.mcp.agent_channel`
# also works.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from src.mcp import agent_channel


class _RecordingEmitter:
    """Fake emit_event that records every call for assertion."""

    def __init__(self):
        self.calls = []

    def __call__(self, event_type, payload, severity="info", chat_id=None):
        self.calls.append(
            {"event_type": event_type, "payload": payload, "severity": severity, "chat_id": chat_id}
        )


class TestSource:
    def test_source_is_local_claude(self):
        assert agent_channel.SOURCE == "local-claude"


class TestWriteReply:
    def test_first_writer_wins_and_creates_slot(self, tmp_path: Path):
        outcome = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-1",
            text="the answer",
            in_reply_to="msg-1",
        )
        assert outcome.reply_slot_created is True
        assert outcome.request_id == "req-1"

        written = json.loads((tmp_path / "req-1.json").read_text())
        assert written["request_id"] == "req-1"
        assert written["text"] == "the answer"
        assert written["in_reply_to"] == "msg-1"
        assert "ts" in written

    def test_in_reply_to_defaults_to_request_id_when_absent(self, tmp_path: Path):
        agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-2",
            text="hi",
            in_reply_to=None,
        )
        written = json.loads((tmp_path / "req-2.json").read_text())
        assert written["in_reply_to"] == "req-2"

    def test_second_writer_loses_race_slot_untouched(self, tmp_path: Path):
        first = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-3",
            text="first answer",
            in_reply_to=None,
        )
        assert first.reply_slot_created is True

        second = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-3",
            text="second answer — should never land",
            in_reply_to=None,
        )
        assert second.reply_slot_created is False

        # Slot content is still the first writer's — never overwritten.
        written = json.loads((tmp_path / "req-3.json").read_text())
        assert written["text"] == "first answer"

    def test_reply_path_is_request_id_json_in_agent_replies_dir(self, tmp_path: Path):
        outcome = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-4",
            text="x",
            in_reply_to=None,
        )
        assert outcome.reply_path == tmp_path / "req-4.json"

    def test_control_chars_round_trip_to_valid_single_line_json(self, tmp_path: Path):
        """Regression test for the reported agent-channel JSON-parse bug.

        bloom (external agent-channel collaborator) reported that replies
        broke its JSON parser with "unterminated string" / zsh "character
        not in range" errors. Investigation (2026-08-05) found the write
        path already escapes control characters correctly via
        ``json.dumps`` (spec-correct regardless of a leaked control byte in
        `text`) — but the file was pretty-printed (`indent=2`, multi-line),
        which silently breaks any external reader that isn't fully
        JSON-aware (e.g. one expecting one JSON object per line). This test
        locks in the fix: the file must be both (a) valid JSON that
        round-trips a `text` value containing raw control characters, and
        (b) written on a single line, so a naive line-oriented reader can
        never see a truncated/partial object.
        """
        nasty_text = "line one\tindented\nline two\rcarriage\x00null\x1bescape"
        agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-nasty",
            text=nasty_text,
            in_reply_to=None,
        )
        raw = (tmp_path / "req-nasty.json").read_text()

        # Single line: no literal newline byte anywhere in the file content
        # (the escaped \n *inside* the JSON string is fine — this checks
        # there's no raw structural newline outside a string).
        assert "\n" not in raw

        written = json.loads(raw)
        assert written["text"] == nasty_text


class TestEmitReplyAudit:
    def test_won_slot_emits_info_severity(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_reply_audit(
            request_id="req-1", reply_slot_created=True, text_len=42, emit_event=emitter
        )
        assert len(emitter.calls) == 1
        call = emitter.calls[0]
        assert call["event_type"] == "agent_channel.reply"
        assert call["severity"] == "info"
        assert call["payload"] == {
            "request_id": "req-1",
            "reply_slot_created": True,
            "text_len": 42,
        }

    def test_lost_race_emits_warn_severity(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_reply_audit(
            request_id="req-1", reply_slot_created=False, text_len=10, emit_event=emitter
        )
        assert emitter.calls[0]["severity"] == "warn"
        assert emitter.calls[0]["payload"]["reply_slot_created"] is False


class TestFormatReplyResponse:
    def test_won_slot_response_includes_preview_and_mark_info(self):
        text = agent_channel.format_reply_response(
            request_id="req-1", text="hello world", reply_slot_created=True, mark_info=" | message m1 marked processed"
        )
        assert "req-1.json" in text
        assert "message m1 marked processed" in text
        assert "hello world" in text
        assert text.startswith("✅")

    def test_won_slot_response_truncates_long_text(self):
        long_text = "x" * 200
        text = agent_channel.format_reply_response(
            request_id="req-1", text=long_text, reply_slot_created=True, mark_info=""
        )
        assert "x" * 100 + "..." in text
        assert "x" * 101 not in text

    def test_lost_race_response_is_no_op_warning(self):
        text = agent_channel.format_reply_response(
            request_id="req-1", text="whatever", reply_slot_created=False, mark_info=""
        )
        assert text.startswith("⚠️ No-op")
        assert "req-1.json" in text
        assert "first writer wins" in text


class TestEmitRequestAudit:
    def test_emits_agent_channel_request_with_ids(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_request_audit(
            request_id="req-1", message_id="msg-1", emit_event=emitter
        )
        assert len(emitter.calls) == 1
        call = emitter.calls[0]
        assert call["event_type"] == "agent_channel.request"
        assert call["payload"] == {"request_id": "req-1", "message_id": "msg-1"}
        assert call["severity"] == "info"  # default


class TestWriteAck:
    def test_success_writes_ack_file_distinct_from_answer_slot(self, tmp_path: Path):
        emitter = _RecordingEmitter()
        response = agent_channel.write_ack(
            agent_replies_dir=tmp_path,
            request_id="req-1",
            ack_text="working on it",
            message_id="msg-1",
            emit_event=emitter,
        )
        ack_file = tmp_path / "req-1.ack.json"
        answer_file = tmp_path / "req-1.json"
        assert ack_file.exists()
        assert not answer_file.exists()  # ack != answer — no code path to the answer slot

        ack_raw = ack_file.read_text()
        assert "\n" not in ack_raw  # compact single-line write — see write_reply's docstring

        written = json.loads(ack_raw)
        assert written == {
            "request_id": "req-1",
            "ack": True,
            "text": "working on it",
            "ts": written["ts"],
        }

        assert "Claimed and acked (local-claude)" in response
        assert "req-1.ack.json" in response
        assert "working on it" in response

        assert len(emitter.calls) == 1
        assert emitter.calls[0]["event_type"] == "agent_channel.ack"
        assert emitter.calls[0]["payload"] == {
            "request_id": "req-1",
            "message_id": "msg-1",
            "text_len": len("working on it"),
        }
        assert emitter.calls[0]["severity"] == "info"

    def test_write_failure_returns_warning_and_emits_warn_severity(self, tmp_path: Path, monkeypatch):
        emitter = _RecordingEmitter()

        def _boom(path, data, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(agent_channel, "atomic_write_json", _boom)

        response = agent_channel.write_ack(
            agent_replies_dir=tmp_path,
            request_id="req-2",
            ack_text="ack text",
            message_id="msg-2",
            emit_event=emitter,
        )
        assert "Warning: message claimed but local-claude ack write failed" in response
        assert "msg-2" in response
        assert "remains in processing/" in response

        assert len(emitter.calls) == 1
        assert emitter.calls[0]["event_type"] == "agent_channel.ack"
        assert emitter.calls[0]["severity"] == "warn"
        assert emitter.calls[0]["payload"]["error"] == "disk full"
        assert not (tmp_path / "req-2.ack.json").exists()
