"""
Tests for /config dispatcher command handlers (issue #1018).

These test pure handler functions — no Telegram, MCP, or network calls required.
All handlers return formatted strings derived from file reads/writes in a tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.orchestration.dispatcher_handlers import (
    handle_config_list,
    handle_config_read,
    handle_config_search,
    handle_config_append,
    _USER_CONFIG_FILENAMES,
    _TELEGRAM_CHAR_LIMIT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary user config directory with sample files."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    return agents_dir


@pytest.fixture
def patched_config_dir(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch _USER_CONFIG_DIR to point at a tmp directory."""
    monkeypatch.setattr(
        "src.orchestration.dispatcher_handlers._USER_CONFIG_DIR", config_dir
    )
    return config_dir


# ---------------------------------------------------------------------------
# handle_config_list
# ---------------------------------------------------------------------------


class TestHandleConfigList:
    """handle_config_list returns a list of user config files with line counts."""

    def test_shows_existing_files(self, patched_config_dir: Path) -> None:
        """Lists files that exist in the config dir."""
        (patched_config_dir / "user.base.bootup.md").write_text(
            "line1\nline2\nline3\n"
        )
        result = handle_config_list()
        assert "user.base.bootup.md" in result
        assert "3 lines" in result

    def test_skips_missing_files(self, patched_config_dir: Path) -> None:
        """Does not list files that don't exist."""
        result = handle_config_list()
        assert "no user config files found" in result

    def test_multiple_files(self, patched_config_dir: Path) -> None:
        """Lists multiple files when they exist."""
        (patched_config_dir / "user.base.bootup.md").write_text("a\nb\n")
        (patched_config_dir / "user.base.context.md").write_text("x\n")
        result = handle_config_list()
        assert "user.base.bootup.md" in result
        assert "user.base.context.md" in result
        assert "2 lines" in result
        assert "1 lines" in result

    def test_header_present(self, patched_config_dir: Path) -> None:
        """Result includes a descriptive header."""
        result = handle_config_list()
        assert "lobster-user-config/agents" in result


# ---------------------------------------------------------------------------
# handle_config_read
# ---------------------------------------------------------------------------


class TestHandleConfigRead:
    """handle_config_read returns (content, needs_chunking) for a config file."""

    def test_reads_existing_file(self, patched_config_dir: Path) -> None:
        """Returns file contents for an allowlisted file that exists."""
        content = "Some bootup content.\nSecond line.\n"
        (patched_config_dir / "user.base.bootup.md").write_text(content)
        result, needs_chunking = handle_config_read("user.base.bootup.md")
        assert result == content
        assert needs_chunking is False

    def test_needs_chunking_for_long_file(self, patched_config_dir: Path) -> None:
        """Sets needs_chunking=True when content exceeds _TELEGRAM_CHAR_LIMIT."""
        long_content = "x" * (_TELEGRAM_CHAR_LIMIT + 100)
        (patched_config_dir / "user.base.bootup.md").write_text(long_content)
        result, needs_chunking = handle_config_read("user.base.bootup.md")
        assert needs_chunking is True
        assert result == long_content

    def test_rejects_non_allowlisted_filename(self, patched_config_dir: Path) -> None:
        """Returns an error for filenames not in the allowlist."""
        result, needs_chunking = handle_config_read("../../etc/passwd")
        assert "Not allowed" in result
        assert needs_chunking is False

    def test_rejects_system_file(self, patched_config_dir: Path) -> None:
        """Does not allow system .claude files."""
        result, needs_chunking = handle_config_read("sys.dispatcher.bootup.md")
        assert "Not allowed" in result
        assert needs_chunking is False

    def test_handles_missing_file(self, patched_config_dir: Path) -> None:
        """Returns a helpful error when an allowlisted file doesn't exist yet."""
        result, needs_chunking = handle_config_read("user.base.context.md")
        assert "not found" in result.lower()
        assert needs_chunking is False

    def test_strips_path_prefix(self, patched_config_dir: Path) -> None:
        """Accepts 'agents/user.base.bootup.md' and strips the prefix."""
        content = "content here"
        (patched_config_dir / "user.base.bootup.md").write_text(content)
        result, _ = handle_config_read("agents/user.base.bootup.md")
        assert result == content


# ---------------------------------------------------------------------------
# handle_config_search
# ---------------------------------------------------------------------------


class TestHandleConfigSearch:
    """handle_config_search searches across all user config files."""

    def test_finds_match(self, patched_config_dir: Path) -> None:
        """Returns matching lines with filename and line number."""
        (patched_config_dir / "user.base.bootup.md").write_text(
            "line one\nremember this setting\nline three\n"
        )
        result = handle_config_search("remember")
        assert "user.base.bootup.md" in result
        assert "remember this setting" in result
        assert "L2" in result

    def test_case_insensitive(self, patched_config_dir: Path) -> None:
        """Search is case-insensitive."""
        (patched_config_dir / "user.base.bootup.md").write_text("Always Do X\n")
        result = handle_config_search("always do x")
        assert "Always Do X" in result

    def test_no_matches_message(self, patched_config_dir: Path) -> None:
        """Returns a 'no matches' message when nothing is found."""
        (patched_config_dir / "user.base.bootup.md").write_text("something else\n")
        result = handle_config_search("zzz_nonexistent_zzz")
        assert "No matches" in result

    def test_empty_query(self, patched_config_dir: Path) -> None:
        """Returns usage hint for empty query."""
        result = handle_config_search("")
        assert "Usage" in result

    def test_searches_multiple_files(self, patched_config_dir: Path) -> None:
        """Returns matches from multiple files."""
        (patched_config_dir / "user.base.bootup.md").write_text("target in bootup\n")
        (patched_config_dir / "user.base.context.md").write_text("target in context\n")
        result = handle_config_search("target")
        assert "user.base.bootup.md" in result
        assert "user.base.context.md" in result

    def test_truncates_at_telegram_limit(self, patched_config_dir: Path) -> None:
        """Long results are truncated with a note."""
        many_matches = "\n".join(["target " + "x" * 100] * 60)
        (patched_config_dir / "user.base.bootup.md").write_text(many_matches)
        result = handle_config_search("target")
        assert len(result) <= _TELEGRAM_CHAR_LIMIT + 100  # some tolerance for note
        assert "truncated" in result


# ---------------------------------------------------------------------------
# handle_config_append
# ---------------------------------------------------------------------------


class TestHandleConfigAppend:
    """handle_config_append appends text to a user config file."""

    def test_appends_to_existing_file(self, patched_config_dir: Path) -> None:
        """Appends to an existing allowlisted file."""
        p = patched_config_dir / "user.base.bootup.md"
        p.write_text("existing content\n")
        result = handle_config_append("user.base.bootup.md", "new setting")
        assert "Appended" in result
        content = p.read_text()
        assert "new setting" in content
        assert "existing content" in content

    def test_creates_file_if_missing(self, patched_config_dir: Path) -> None:
        """Creates the file if it doesn't exist yet (for allowlisted filenames)."""
        result = handle_config_append("user.base.context.md", "new context entry")
        assert "Appended" in result
        p = patched_config_dir / "user.base.context.md"
        assert p.exists()
        assert "new context entry" in p.read_text()

    def test_rejects_non_allowlisted_filename(self, patched_config_dir: Path) -> None:
        """Returns an error for filenames not in the allowlist."""
        result = handle_config_append("../../etc/passwd", "injected")
        assert "Not allowed" in result

    def test_empty_text_returns_usage(self, patched_config_dir: Path) -> None:
        """Returns usage hint when text is empty."""
        result = handle_config_append("user.base.bootup.md", "")
        assert "Usage" in result

    def test_shows_tail_in_confirmation(self, patched_config_dir: Path) -> None:
        """Confirmation message includes the tail of the file."""
        p = patched_config_dir / "user.base.bootup.md"
        p.write_text("line1\n")
        result = handle_config_append("user.base.bootup.md", "my new line")
        assert "my new line" in result
        assert "Tail" in result
