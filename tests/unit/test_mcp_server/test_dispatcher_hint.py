"""
Tests for dispatcher_hint in check_inbox / wait_for_messages

Verifies that messages with file attachments (voice, photo, document) get a
plain-ASCII dispatcher_hint line, and that plain text messages do not.
"""

import asyncio
import json
import sys
from pathlib import Path
import pytest
from unittest.mock import patch

# inbox_server imports sibling modules (reliability, update_manager, etc.) from
# src/mcp/, so we must add that directory to sys.path before importing it.
_MCP_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

# Pre-import so patch.multiple("src.mcp.inbox_server", ...) can resolve the target.
import src.mcp.inbox_server  # noqa: E402


class TestDispatcherHint:
    """Unit tests for dispatcher_hint feature."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def _check_inbox(self, inbox_dir: Path) -> str:
        """Helper: run handle_check_inbox and return the result text."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    # ------------------------------------------------------------------
    # Hint PRESENT for file-bearing messages
    # ------------------------------------------------------------------

    def test_voice_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Voice messages should include the dispatcher_hint line."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_photo_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Photo messages should include the dispatcher_hint line."""
        msg = message_generator.generate_photo_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_photo_message_multi_image_gets_hint(self, inbox_dir: Path, message_generator):
        """Photo messages with multiple images should include the dispatcher_hint line."""
        msg = message_generator.generate_photo_message(
            image_files=["/tmp/photo_a.jpg", "/tmp/photo_b.jpg"]
        )
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_document_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Document messages should include the dispatcher_hint line."""
        msg = message_generator.generate_document_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_image_file_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have an image_file field get the hint."""
        msg = message_generator.generate_text_message()
        msg["image_file"] = "/tmp/images/some_image.jpg"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_file_path_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have a file_path field get the hint."""
        msg = message_generator.generate_text_message()
        msg["file_path"] = "/tmp/files/attachment.pdf"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_audio_file_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have an audio_file field get the hint."""
        msg = message_generator.generate_text_message()
        msg["audio_file"] = "/tmp/audio/clip.ogg"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    # ------------------------------------------------------------------
    # Hint ABSENT for plain messages
    # ------------------------------------------------------------------

    def test_plain_text_message_no_hint(self, inbox_dir: Path, message_generator):
        """Plain text messages must NOT include the dispatcher_hint line."""
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint" not in text

    # ------------------------------------------------------------------
    # Hint is plain ASCII -- no Unicode characters
    # ------------------------------------------------------------------

    def test_hint_contains_no_unicode(self, inbox_dir: Path, message_generator):
        """The dispatcher_hint value must be pure ASCII (no emoji or Unicode)."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        # Extract the dispatcher_hint line
        hint_line = next(
            (line for line in text.splitlines() if line.startswith("dispatcher_hint:")),
            None,
        )
        assert hint_line is not None, "dispatcher_hint line not found"
        # Verify it's pure ASCII -- will raise UnicodeEncodeError if not
        hint_line.encode("ascii")

    # ------------------------------------------------------------------
    # Multiple messages in inbox
    # ------------------------------------------------------------------

    def test_mixed_inbox_hints_only_on_file_messages(
        self, inbox_dir: Path, message_generator
    ):
        """Only file-bearing messages get a hint; text messages do not."""
        text_msg = message_generator.generate_text_message()
        voice_msg = message_generator.generate_voice_message()
        (inbox_dir / f"{text_msg['id']}.json").write_text(json.dumps(text_msg))
        (inbox_dir / f"{voice_msg['id']}.json").write_text(json.dumps(voice_msg))

        text = self._check_inbox(inbox_dir)

        # Count hint occurrences -- exactly one (for the voice message)
        hint_count = text.count("dispatcher_hint: HINT: file attached - use subagent")
        assert hint_count == 1


class TestWosExecuteMessageFormatting:
    """Tests for wos_execute message type formatting in check_inbox (issue #677).

    wos_execute messages have no text field — the dispatcher would see '(no text)'
    and fail to route them. This validates that:
    1. A synthetic text summary is shown (not '(no text)')
    2. The dispatcher_hint tells the dispatcher to call route_wos_message
    3. The uow_id is surfaced in both the header and the hint
    """

    WOS_EXECUTE_HINT_PREFIX = "dispatcher_hint: WOS_EXECUTE"

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def _build_wos_execute_msg(self, uow_id: str = "test-uow-001") -> dict:
        """Build a minimal wos_execute inbox message (mirrors executor._dispatch_via_inbox)."""
        import uuid
        from datetime import datetime, timezone
        return {
            "id": str(uuid.uuid4()),
            "source": "system",
            "type": "wos_execute",
            "chat_id": "8075091586",
            "uow_id": uow_id,
            "instructions": "Execute this unit of work.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _check_inbox(self, inbox_dir: Path) -> str:
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    def test_wos_execute_text_summary_shown_not_no_text(self, inbox_dir: Path) -> None:
        """wos_execute message must not show '(no text)' — a synthetic summary is required."""
        uow_id = "uow-abc-123"
        msg = self._build_wos_execute_msg(uow_id=uow_id)
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "(no text)" not in text
        assert uow_id in text

    def test_wos_execute_text_summary_contains_type_prefix(self, inbox_dir: Path) -> None:
        """Synthetic text summary must include 'wos_execute:' so pattern-matching works."""
        msg = self._build_wos_execute_msg(uow_id="uow-xyz-999")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "wos_execute: uow_id=uow-xyz-999" in text

    def test_wos_execute_dispatcher_hint_present(self, inbox_dir: Path) -> None:
        """wos_execute must emit a WOS_EXECUTE dispatcher_hint for structural routing."""
        uow_id = "uow-hint-test"
        msg = self._build_wos_execute_msg(uow_id=uow_id)
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert self.WOS_EXECUTE_HINT_PREFIX in text

    def test_wos_execute_hint_mentions_route_wos_message(self, inbox_dir: Path) -> None:
        """Dispatcher hint must tell the dispatcher to call route_wos_message."""
        msg = self._build_wos_execute_msg()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "route_wos_message" in text

    def test_wos_execute_hint_includes_uow_id(self, inbox_dir: Path) -> None:
        """Dispatcher hint must include the uow_id so the dispatcher can correlate."""
        uow_id = "uow-correlation-id"
        msg = self._build_wos_execute_msg(uow_id=uow_id)
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        hint_line = next(
            (line for line in text.splitlines() if line.startswith("dispatcher_hint: WOS_EXECUTE")),
            None,
        )
        assert hint_line is not None, "WOS_EXECUTE dispatcher_hint line not found"
        assert uow_id in hint_line

    def test_wos_execute_header_icon_shown(self, inbox_dir: Path) -> None:
        """wos_execute messages must show the [WOS EXECUTE] header, not a generic sender."""
        uow_id = "uow-header-test"
        msg = self._build_wos_execute_msg(uow_id=uow_id)
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "[WOS EXECUTE]" in text
