"""
Tests for follow-up compaction (_maybe_compact_follow_ups).

Covers:
- Primary signal: reply threading (reply_to.message_id points to a prior inbox message)
- Secondary signal: time window (≤8s) + negation marker
- Pass-through: messages that don't match either signal
- Non-text messages are not compacted
- Output format: compact_group schema and check_inbox display
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from unittest.mock import patch

# Add src/mcp/ to sys.path so inbox_server imports work.
_MCP_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import src.mcp.inbox_server as _inbox_mod
from src.mcp.inbox_server import (
    _maybe_compact_follow_ups,
    _has_negation_marker,
    _seconds_between,
    _build_compact_group,
    _find_original_message,
    NEGATION_MARKERS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_seconds: float = 0) -> str:
    """Return an ISO 8601 UTC timestamp offset by `offset_seconds` from now."""
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return t.isoformat()


def _text_msg(
    msg_id: str,
    text: str,
    chat_id: int = 1111,
    tg_message_id: int | None = None,
    timestamp: str | None = None,
    reply_to: dict | None = None,
) -> dict:
    """Build a minimal text-type inbox message dict."""
    return {
        "id": msg_id,
        "source": "telegram",
        "type": "text",
        "chat_id": chat_id,
        "user_id": chat_id,
        "username": "testuser",
        "user_name": "Test",
        "text": text,
        "telegram_message_id": tg_message_id or int(msg_id.split("_")[0]),
        "timestamp": timestamp or _ts(),
        **({"reply_to": reply_to} if reply_to else {}),
    }


# ---------------------------------------------------------------------------
# Unit: _has_negation_marker
# ---------------------------------------------------------------------------


class TestHasNegationMarker:
    def test_leading_marker(self):
        assert _has_negation_marker("actually, forget it") is True

    def test_leading_wait_marker(self):
        # "wait" appears at the start — should fire
        assert _has_negation_marker("wait, I meant something else") is True

    def test_mid_sentence_wait_does_not_fire(self):
        # "wait" buried mid-sentence should NOT trigger compaction (false-positive guard)
        assert _has_negation_marker("I'll wait for you") is False

    def test_mid_sentence_actually_does_not_fire(self):
        assert _has_negation_marker("that's actually fine") is False

    def test_no_marker(self):
        assert _has_negation_marker("please do the task") is False

    def test_case_insensitive(self):
        assert _has_negation_marker("ACTUALLY never mind") is True

    def test_all_defined_markers(self):
        for marker in NEGATION_MARKERS:
            assert _has_negation_marker(marker + " do something") is True, (
                f"marker {marker!r} was not detected"
            )

    def test_empty_string(self):
        assert _has_negation_marker("") is False


# ---------------------------------------------------------------------------
# Unit: _seconds_between
# ---------------------------------------------------------------------------


class TestSecondsBetween:
    def test_two_seconds_apart(self):
        t1 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        t2 = datetime(2026, 3, 1, 12, 0, 2, tzinfo=timezone.utc).isoformat()
        assert _seconds_between(t1, t2) == pytest.approx(2.0)

    def test_order_independent(self):
        t1 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        t2 = datetime(2026, 3, 1, 12, 0, 5, tzinfo=timezone.utc).isoformat()
        assert _seconds_between(t2, t1) == pytest.approx(5.0)

    def test_invalid_timestamp_returns_none(self):
        assert _seconds_between("not-a-timestamp", "2026-03-01T00:00:00Z") is None


# ---------------------------------------------------------------------------
# Unit: _build_compact_group
# ---------------------------------------------------------------------------


class TestBuildCompactGroup:
    def test_type_is_compact_group(self):
        orig = _text_msg("100_1", "Do X")
        follow = _text_msg("100_2", "Actually do Y", reply_to={"message_id": 1})
        result = _build_compact_group(orig, follow, "reply_thread")
        assert result["type"] == "compact_group"

    def test_messages_list_has_two_items(self):
        orig = _text_msg("100_1", "Do X")
        follow = _text_msg("100_2", "Actually do Y")
        result = _build_compact_group(orig, follow, "negation_marker")
        assert len(result["messages"]) == 2

    def test_messages_preserve_text(self):
        orig = _text_msg("100_1", "Do X")
        follow = _text_msg("100_2", "Actually do Y")
        result = _build_compact_group(orig, follow, "negation_marker")
        assert result["messages"][0]["text"] == "Do X"
        assert result["messages"][1]["text"] == "Actually do Y"

    def test_compact_reason_stored(self):
        orig = _text_msg("100_1", "Do X")
        follow = _text_msg("100_2", "Actually do Y")
        result = _build_compact_group(orig, follow, "reply_thread")
        assert result["compact_reason"] == "reply_thread"

    def test_envelope_uses_follow_up_chat_id(self):
        orig = _text_msg("100_1", "Do X", chat_id=9999)
        follow = _text_msg("100_2", "Actually do Y", chat_id=9999)
        result = _build_compact_group(orig, follow, "reply_thread")
        assert result["chat_id"] == 9999

    def test_text_field_is_summary(self):
        orig = _text_msg("100_1", "Do X")
        follow = _text_msg("100_2", "Actually do Y")
        result = _build_compact_group(orig, follow, "negation_marker")
        assert "Compact group" in result["text"]
        assert "2 messages" in result["text"]


# ---------------------------------------------------------------------------
# Unit: _maybe_compact_follow_ups — reply threading signal
# ---------------------------------------------------------------------------


class TestMaybeCompactFollowUpsReplyThread:
    def test_reply_triggers_compaction(self):
        """Follow-up that replies to a prior message in the same batch is compacted."""
        orig = _text_msg("100_1", "Do X", tg_message_id=40)
        follow = _text_msg(
            "100_2",
            "Actually do Y instead",
            reply_to={"message_id": 40},
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 1
        assert result[0]["type"] == "compact_group"

    def test_compact_group_preserves_both_texts(self):
        orig = _text_msg("100_1", "Write a short email", tg_message_id=41)
        follow = _text_msg(
            "100_2",
            "Actually make it longer",
            reply_to={"message_id": 41},
        )
        result = _maybe_compact_follow_ups([orig, follow])
        texts = [m["text"] for m in result[0]["messages"]]
        assert "Write a short email" in texts
        assert "Actually make it longer" in texts

    def test_compact_reason_is_reply_thread(self):
        orig = _text_msg("100_1", "Do X", tg_message_id=42)
        follow = _text_msg("100_2", "Actually do Y", reply_to={"message_id": 42})
        result = _maybe_compact_follow_ups([orig, follow])
        assert result[0]["compact_reason"] == "reply_thread"

    def test_unrelated_reply_not_compacted(self):
        """Reply to a message not in the batch passes through unchanged (no compaction)."""
        now = datetime.now(timezone.utc)
        orig = _text_msg("100_1", "Do X", tg_message_id=50, timestamp=now.isoformat())
        # "Make it longer" has no negation marker, so the fallback signal can't fire either.
        # The reply_to points to message_id 99, which is not in the batch.
        follow = _text_msg(
            "100_2",
            "Make it longer",
            reply_to={"message_id": 99},
            timestamp=(now + timedelta(seconds=2)).isoformat(),
        )
        # message_id 99 is not in the batch and not on disk (patch _find_original_message)
        with patch.object(_inbox_mod, "_find_original_message", return_value=None):
            result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 2
        assert all(m["type"] == "text" for m in result)

    def test_non_text_original_not_compacted(self):
        """Voice message as original is not compacted even if referenced by reply."""
        voice_msg = _text_msg("100_1", "[Voice]", tg_message_id=60)
        voice_msg["type"] = "voice"
        follow = _text_msg("100_2", "Actually cancel that", reply_to={"message_id": 60})
        result = _maybe_compact_follow_ups([voice_msg, follow])
        assert len(result) == 2
        assert result[0]["type"] == "voice"


# ---------------------------------------------------------------------------
# Unit: _maybe_compact_follow_ups — negation marker + time window signal
# ---------------------------------------------------------------------------


class TestMaybeCompactFollowUpsNegationMarker:
    def test_negation_within_window_triggers_compaction(self):
        now = datetime.now(timezone.utc)
        orig = _text_msg("200_1", "Do X", timestamp=now.isoformat())
        follow = _text_msg(
            "200_2",
            "actually, do Y instead",
            timestamp=(now + timedelta(seconds=5)).isoformat(),
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 1
        assert result[0]["type"] == "compact_group"
        assert result[0]["compact_reason"] == "negation_marker"

    def test_negation_outside_window_not_compacted(self):
        now = datetime.now(timezone.utc)
        orig = _text_msg("200_1", "Do X", timestamp=now.isoformat())
        follow = _text_msg(
            "200_2",
            "actually, do Y instead",
            # 20 seconds later — outside the 8s window
            timestamp=(now + timedelta(seconds=20)).isoformat(),
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 2

    def test_negation_different_chat_not_compacted(self):
        now = datetime.now(timezone.utc)
        orig = _text_msg("200_1", "Do X", chat_id=1111, timestamp=now.isoformat())
        follow = _text_msg(
            "200_2",
            "actually, do Y",
            chat_id=2222,
            timestamp=(now + timedelta(seconds=2)).isoformat(),
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 2

    def test_no_negation_no_compaction(self):
        now = datetime.now(timezone.utc)
        orig = _text_msg("200_1", "Do X", timestamp=now.isoformat())
        follow = _text_msg(
            "200_2",
            "and also do Z",
            timestamp=(now + timedelta(seconds=3)).isoformat(),
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result) == 2

    def test_compact_group_has_two_constituent_messages(self):
        now = datetime.now(timezone.utc)
        orig = _text_msg("200_1", "Do X", timestamp=now.isoformat())
        follow = _text_msg(
            "200_2",
            "wait, actually do Y",
            timestamp=(now + timedelta(seconds=4)).isoformat(),
        )
        result = _maybe_compact_follow_ups([orig, follow])
        assert len(result[0]["messages"]) == 2


# ---------------------------------------------------------------------------
# Unit: _maybe_compact_follow_ups — pass-through cases
# ---------------------------------------------------------------------------


class TestMaybeCompactFollowUpsPassThrough:
    def test_single_message_unchanged(self):
        msg = _text_msg("300_1", "Hello")
        result = _maybe_compact_follow_ups([msg])
        assert result == [msg]

    def test_empty_list_unchanged(self):
        assert _maybe_compact_follow_ups([]) == []

    def test_non_text_messages_passed_through(self):
        voice = _text_msg("300_1", "[Voice]")
        voice["type"] = "voice"
        photo = _text_msg("300_2", "[Photo]")
        photo["type"] = "photo"
        result = _maybe_compact_follow_ups([voice, photo])
        assert len(result) == 2
        assert result[0]["type"] == "voice"
        assert result[1]["type"] == "photo"

    def test_subagent_result_not_compacted(self):
        msg = {
            "id": "300_3",
            "type": "subagent_result",
            "source": "telegram",
            "chat_id": 1111,
            "text": "Done!",
            "timestamp": _ts(),
        }
        result = _maybe_compact_follow_ups([msg])
        assert result == [msg]


# ---------------------------------------------------------------------------
# Integration: handle_check_inbox formats compact_group correctly
# ---------------------------------------------------------------------------


class TestCheckInboxCompactGroupDisplay:
    """Verify that handle_check_inbox renders compact_group messages with the
    correct dispatcher_hint and constituent messages."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def _check_inbox(self, inbox_dir: Path) -> str:
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            PROCESSING_DIR=inbox_dir.parent / "processing",
        ):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    def test_compact_group_hint_present(self, inbox_dir: Path):
        """compact_group messages must include the COMPACT_GROUP dispatcher_hint."""
        now = datetime.now(timezone.utc)
        orig = _text_msg("400_1", "Do X", timestamp=now.isoformat())
        follow = _text_msg(
            "400_2",
            "actually do Y",
            timestamp=(now + timedelta(seconds=3)).isoformat(),
        )
        # Write messages to inbox
        (inbox_dir / "400_1.json").write_text(json.dumps(orig))
        (inbox_dir / "400_2.json").write_text(json.dumps(follow))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: COMPACT_GROUP" in text

    def test_compact_group_constituent_texts_displayed(self, inbox_dir: Path):
        """Both constituent message texts must appear in the output."""
        now = datetime.now(timezone.utc)
        orig = _text_msg("401_1", "Write a short summary", timestamp=now.isoformat())
        follow = _text_msg(
            "401_2",
            "actually make it longer",
            timestamp=(now + timedelta(seconds=2)).isoformat(),
        )
        (inbox_dir / "401_1.json").write_text(json.dumps(orig))
        (inbox_dir / "401_2.json").write_text(json.dumps(follow))

        text = self._check_inbox(inbox_dir)

        assert "Write a short summary" in text
        assert "actually make it longer" in text

    def test_compact_group_reduces_message_count(self, inbox_dir: Path):
        """Two compactable messages appear as one compact_group in the output."""
        now = datetime.now(timezone.utc)
        orig = _text_msg("402_1", "Do the thing", timestamp=now.isoformat())
        follow = _text_msg(
            "402_2",
            "wait, cancel that",
            timestamp=(now + timedelta(seconds=4)).isoformat(),
        )
        (inbox_dir / "402_1.json").write_text(json.dumps(orig))
        (inbox_dir / "402_2.json").write_text(json.dumps(follow))

        text = self._check_inbox(inbox_dir)

        # "1 new message" not "2 new messages"
        assert "1 new message" in text

    def test_plain_text_not_compacted_in_output(self, inbox_dir: Path):
        """Unrelated messages in different chats are not merged."""
        now = datetime.now(timezone.utc)
        msg_a = _text_msg("403_1", "Hello from A", chat_id=1111, timestamp=now.isoformat())
        msg_b = _text_msg(
            "403_2",
            "actually hello from B",
            chat_id=2222,
            timestamp=(now + timedelta(seconds=2)).isoformat(),
        )
        (inbox_dir / "403_1.json").write_text(json.dumps(msg_a))
        (inbox_dir / "403_2.json").write_text(json.dumps(msg_b))

        text = self._check_inbox(inbox_dir)

        assert "2 new message" in text
        assert "compact_group" not in text.lower().replace("COMPACT_GROUP", "")

    def test_compact_group_original_filename_present(self, inbox_dir: Path):
        """check_inbox output for a compact_group must include _original_filename
        so the dispatcher can extract message_id without parsing a filepath."""
        now = datetime.now(timezone.utc)
        orig = _text_msg("404_1", "Do the thing", timestamp=now.isoformat())
        follow = _text_msg(
            "404_2",
            "actually cancel that",
            timestamp=(now + timedelta(seconds=3)).isoformat(),
        )
        (inbox_dir / "404_1.json").write_text(json.dumps(orig))
        (inbox_dir / "404_2.json").write_text(json.dumps(follow))

        text = self._check_inbox(inbox_dir)

        assert "_original_filename" in text
        assert "404_1.json" in text
