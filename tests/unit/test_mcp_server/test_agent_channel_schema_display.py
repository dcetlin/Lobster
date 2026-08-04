"""
Tests for agent-channel / non-standard-schema message display in
check_inbox / wait_for_messages.

Some inbound messages (e.g. glyph's health_check pings) use id/subject/body
instead of the standard chat_id/message_id/text schema and carry no
user_name/username field. Before this fix, they fell through the formatter's
defaults and rendered as "[SYSTEM] from **Unknown**" with an empty chat_id
and "(no text)" instead of the actual body content. This module verifies the
non-standard schema is detected and rendered legibly, and that standard
telegram/slack/system text messages are unaffected.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
import pytest
from unittest.mock import patch

# inbox_server imports sibling modules from src/mcp/, so ensure that directory
# is on sys.path before importing.
_MCP_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import src.mcp.inbox_server  # noqa: E402  (side-effect: registers module for patching)


def _make_agent_channel_msg(
    subject: str = "Claude Code session (glyph) — introduction",
    body: str = "I am a Claude Code session on Dan's laptop.",
    source: str = "system",
    msg_type: str = "health_check",
) -> dict:
    """Build a minimal id/subject/body-schema message dict (e.g. glyph's health_check pings)."""
    ts_ms = int(time.time() * 1000)
    return {
        "id": f"dancetlin-glyph-{ts_ms}",
        "type": msg_type,
        "source": source,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "subject": subject,
        "body": body,
        "severity": "info",
    }


def _make_standard_text_msg(text: str = "hello", chat_id: int = 100001) -> dict:
    """Build a minimal standard telegram text message dict for regression coverage."""
    ts_ms = int(time.time() * 1000)
    return {
        "id": f"{ts_ms}_text_test",
        "type": "text",
        "source": "telegram",
        "chat_id": chat_id,
        "user_name": "Dan",
        "text": text,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }


class TestAgentChannelSchemaDisplay:
    """check_inbox must render id/subject/body-schema messages legibly."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def _check_inbox(self, inbox_dir: Path) -> str:
        with patch.multiple("src.mcp.inbox_server", INBOX_DIR=inbox_dir):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    # ------------------------------------------------------------------
    # Non-standard schema renders legibly
    # ------------------------------------------------------------------

    def test_no_system_from_unknown(self, inbox_dir: Path):
        """The old '[SYSTEM] from **Unknown**' default must not appear."""
        msg = _make_agent_channel_msg()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "from **Unknown**" not in text

    def test_source_populated(self, inbox_dir: Path):
        """Source bracket must still show a real source, not blank."""
        msg = _make_agent_channel_msg()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "[SYSTEM]" in text

    def test_message_id_populated(self, inbox_dir: Path):
        """The id field must be shown as the Message ID (not blank)."""
        msg = _make_agent_channel_msg()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert f"Message ID: `{msg['id']}`" in text

    def test_body_shown_as_text(self, inbox_dir: Path):
        """The body field must be rendered as the message's displayed text."""
        msg = _make_agent_channel_msg(body="unique body payload xyz123")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "unique body payload xyz123" in text
        assert "(no text)" not in text

    def test_subject_shown_as_heading(self, inbox_dir: Path):
        """The subject field must be surfaced as a heading/title."""
        msg = _make_agent_channel_msg(subject="unique subject heading abc789")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "unique subject heading abc789" in text

    def test_chat_id_not_blank(self, inbox_dir: Path):
        """Chat ID must not render as an empty backtick pair for this schema."""
        msg = _make_agent_channel_msg()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "Chat ID: ``" not in text

    # ------------------------------------------------------------------
    # Regression: standard schema messages are unaffected
    # ------------------------------------------------------------------

    def test_standard_text_message_unaffected(self, inbox_dir: Path):
        """A normal telegram text message must render exactly as before."""
        msg = _make_standard_text_msg(text="hello world")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "[TELEGRAM]" in text
        assert "from **Dan**" in text
        assert "hello world" in text
        assert "Chat ID: `100001`" in text
        assert "agent-channel message" not in text
