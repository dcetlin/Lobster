"""
Bot-layer pre-handler for deterministic Telegram commands.

Routing rule: output fully determined by a fixed DB/file query → pre-handler.
Output that needs LLM context or judgment → dispatcher.

Commands handled here (bypasses inbox entirely):
  /todos   — open LOS action items from self_action_items.db
  /quota   — CC usage from ~/.claude/cc-budget/state.json
  /status  — WOS state + active agents + CC usage (file reads + session DB)

WOS-UoW: uow_20260515_b782a7
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

log = logging.getLogger(__name__)

# Read once at module load — same env var lobster_bot uses.
_ALLOWED_USERS: frozenset[int] = frozenset(
    int(x)
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip()
)


def _priority_label(priority: int) -> str:
    if priority <= 3:
        return "urgent"
    if priority <= 6:
        return "medium"
    return "low"


async def handle_todos_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /todos — reads self_action_items.db, sends with buttons."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return

    from los.db import connect, get_open_items  # noqa: PLC0415

    conn = connect()
    try:
        items = get_open_items(conn, limit=10)
    finally:
        conn.close()

    if not items:
        await update.message.reply_text("No open action items. Great job!")
        return

    await update.message.reply_text(f"You have {len(items)} open action item(s):")

    today = date.today()
    snooze_3d = (today + timedelta(days=3)).isoformat()
    snooze_1w = (today + timedelta(weeks=1)).isoformat()

    for item in items:
        label = _priority_label(item.priority)
        text = f"[{label}] {item.text}\n(source: {item.source})"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Done", callback_data=f"todo-done-{item.id}"),
                InlineKeyboardButton("Dismiss", callback_data=f"todo-dismiss-{item.id}"),
            ],
            [
                InlineKeyboardButton("Snooze 3d", callback_data=f"todo-snooze-{item.id}-{snooze_3d}"),
                InlineKeyboardButton("Snooze 1w", callback_data=f"todo-snooze-{item.id}-{snooze_1w}"),
            ],
        ])
        await update.message.reply_text(text, reply_markup=keyboard)


async def handle_quota_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /quota — reads cc-budget/state.json, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return

    from orchestration.dispatcher_handlers import format_quota_message, read_quota_state  # noqa: PLC0415

    state = read_quota_state()
    msg = format_quota_message(state)
    await update.message.reply_text(msg)


async def handle_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /status — reads files + session DB, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return

    from agents.session_store import get_active_sessions  # noqa: PLC0415
    from orchestration.dispatcher_handlers import (  # noqa: PLC0415
        format_status_message,
        read_quota_state,
        read_wos_config,
    )
    from orchestration.registry import Registry  # noqa: PLC0415

    quota_state = read_quota_state()
    wos_config = read_wos_config()

    try:
        active_sessions = get_active_sessions()
    except Exception:
        active_sessions = []

    try:
        registry = Registry()
        status_counts = registry.get_status_counts()
    except Exception:
        status_counts = {}

    msg = format_status_message(
        active_sessions=active_sessions,
        wos_config=wos_config,
        status_counts=status_counts,
        quota_state=quota_state,
    )
    await update.message.reply_text(msg)
