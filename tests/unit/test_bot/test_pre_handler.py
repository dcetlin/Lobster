"""Smoke tests for the bot pre-handler module.

WOS-UoW: uow_20260515_b782a7
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_priority_label():
    from bot.pre_handler import _priority_label

    assert _priority_label(1) == "urgent"
    assert _priority_label(3) == "urgent"
    assert _priority_label(4) == "medium"
    assert _priority_label(6) == "medium"
    assert _priority_label(7) == "low"
    assert _priority_label(9) == "low"


def test_module_exports_three_handlers():
    import bot.pre_handler as ph

    assert callable(ph.handle_todos_command)
    assert callable(ph.handle_quota_command)
    assert callable(ph.handle_status_command)


@pytest.mark.asyncio
async def test_handle_quota_command_unauthorized():
    """Unauthorized user gets no reply."""
    from bot.pre_handler import handle_quota_command

    update = MagicMock()
    update.effective_user.id = 99999
    context = MagicMock()

    with patch("bot.pre_handler._ALLOWED_USERS", frozenset({12345})):
        result = await handle_quota_command(update, context)

    update.message.reply_text.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_handle_status_command_unauthorized():
    """Unauthorized user gets no reply."""
    from bot.pre_handler import handle_status_command

    update = MagicMock()
    update.effective_user.id = 99999
    context = MagicMock()

    with patch("bot.pre_handler._ALLOWED_USERS", frozenset({12345})):
        result = await handle_status_command(update, context)

    update.message.reply_text.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_handle_todos_command_unauthorized():
    """Unauthorized user gets no reply."""
    from bot.pre_handler import handle_todos_command

    update = MagicMock()
    update.effective_user.id = 99999
    context = MagicMock()

    with patch("bot.pre_handler._ALLOWED_USERS", frozenset({12345})):
        result = await handle_todos_command(update, context)

    update.message.reply_text.assert_not_called()
    assert result is None
