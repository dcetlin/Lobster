"""
Bot-layer pre-handler for deterministic Telegram commands.

Routing rule: output fully determined by a fixed DB/file query → pre-handler.
Output that needs LLM context or judgment → dispatcher.

Commands handled here (bypasses inbox entirely):
  /todos      — open LOS action items from self_action_items.db
  /quota      — CC usage from ~/.claude/cc-budget/state.json
  /status     — WOS state + active agents + CC usage (file reads + session DB)
  /subagents  — active subagent sessions from session store
  /jobs       — scheduled jobs from jobs.json
  /wos        — WOS queue counts and dashboard link
  /restart    — restart dispatcher (with confirmation) or warn for mcp/all

WOS-UoW: uow_20260515_75d522
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

if TYPE_CHECKING:
    from telegram import CallbackQuery, Message

log = logging.getLogger(__name__)

# Read once at module load — same env var lobster_bot uses.
_ALLOWED_USERS: frozenset[int] = frozenset(
    int(x)
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip()
)

PRE_HANDLER_COMMANDS: frozenset[str] = frozenset({
    "/todos", "/quota", "/status",
    "/subagents", "/jobs", "/wos", "/restart",
})


def _priority_label(priority: int) -> str:
    if priority <= 3:
        return "urgent"
    if priority <= 6:
        return "medium"
    return "low"


async def try_handle(
    text: str,
    message: "Message",
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Return True and send a reply if text is a whitelisted deterministic command.

    Returns False immediately if text is not in PRE_HANDLER_COMMANDS, so the
    caller can fall through to the normal inbox path without any branching.
    """
    parts = text.strip().lower().split()
    cmd = parts[0] if parts else ""
    args = parts[1:]

    if cmd not in PRE_HANDLER_COMMANDS:
        return False

    try:
        if cmd == "/todos":
            await _handle_todos(message)
        elif cmd == "/quota":
            await _handle_quota(message)
        elif cmd == "/status":
            await _handle_status(message)
        elif cmd == "/subagents":
            await _handle_subagents(message)
        elif cmd == "/jobs":
            await _handle_jobs(message)
        elif cmd == "/wos":
            await _handle_wos(message)
        elif cmd == "/restart":
            await _handle_restart(message, args)
    except Exception as exc:
        log.error("pre_handler failed for %s: %s", cmd, exc, exc_info=True)
        await message.reply_text(f"⚠️ {cmd} failed: {exc}")

    return True


# ---------------------------------------------------------------------------
# Update-style command handlers (registered via add_handler in lobster_bot.py)
# ---------------------------------------------------------------------------

async def handle_todos_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /todos — reads self_action_items.db, sends with buttons."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_todos(update.message)


async def handle_quota_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /quota — reads cc-budget/state.json, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_quota(update.message)


async def handle_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /status — reads files + session DB, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_status(update.message)


async def handle_help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /help — static command index, no I/O."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    from orchestration.dispatcher_handlers import handle_help  # noqa: PLC0415
    await update.message.reply_text(handle_help())


async def handle_subagents_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /subagents — active session list, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_subagents(update.message)


async def handle_jobs_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /jobs — scheduled jobs from jobs.json, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_jobs(update.message)


async def handle_wos_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /wos — WOS queue counts and dashboard link, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    await _handle_wos(update.message)


async def handle_restart_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pre-handler for /restart — confirmation gate or safety warning, bypasses dispatcher."""
    user = update.effective_user
    if not user or user.id not in _ALLOWED_USERS:
        return
    parts = (update.message.text or "").strip().split()
    args = parts[1:]  # everything after /restart
    await _handle_restart(update.message, args)


# ---------------------------------------------------------------------------
# Message-based private implementations (used by try_handle and command handlers)
# ---------------------------------------------------------------------------

async def _handle_todos(message: "Message") -> None:
    from los.db import connect, get_open_items  # noqa: PLC0415

    conn = connect()
    try:
        items = get_open_items(conn, limit=10)
    finally:
        conn.close()

    if not items:
        await message.reply_text("No open action items. Great job!")
        return

    await message.reply_text(f"You have {len(items)} open action item(s):")

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
        await message.reply_text(text, reply_markup=keyboard)


async def _handle_quota(message: "Message") -> None:
    from orchestration.dispatcher_handlers import format_quota_message, read_quota_state  # noqa: PLC0415

    state = read_quota_state()
    msg = format_quota_message(state)
    await message.reply_text(msg)


async def _handle_status(message: "Message") -> None:
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
    await message.reply_text(msg)


async def _handle_subagents(message: "Message") -> None:
    from agents.session_store import get_active_sessions  # noqa: PLC0415

    sessions = get_active_sessions()
    if not sessions:
        await message.reply_text("No active subagents.")
        return
    lines = [f"Active subagents ({len(sessions)}):"]
    for s in sessions:
        agent_type = s.get("agent_type", "unknown")
        task_id = s.get("task_id") or s.get("agent_id", "?")
        description = s.get("description", "")
        lines.append(
            f"• {task_id} — {agent_type}"
            + (f": {description[:60]}" if description else "")
        )
    await message.reply_text("\n".join(lines))


async def _handle_jobs(message: "Message") -> None:
    import json  # noqa: PLC0415

    jobs_path = Path.home() / "lobster-workspace" / "scheduled-jobs" / "jobs.json"
    try:
        data = json.loads(jobs_path.read_text())
    except Exception as exc:
        await message.reply_text(f"Could not read jobs registry: {exc}")
        return
    jobs = data.get("jobs", {})
    if not jobs:
        await message.reply_text("No scheduled jobs registered.")
        return
    enabled = [(name, j) for name, j in jobs.items() if j.get("enabled")]
    disabled = [(name, j) for name, j in jobs.items() if not j.get("enabled")]
    lines = [f"Jobs: {len(enabled)} enabled, {len(disabled)} disabled"]
    for name, j in sorted(enabled):
        schedule = j.get("schedule", "?")
        lines.append(f"[on]  {name} — {schedule}")
    for name, j in sorted(disabled):
        schedule = j.get("schedule", "?")
        lines.append(f"[off] {name} — {schedule}")
    await message.reply_text("\n".join(lines))


async def _handle_wos(message: "Message") -> None:
    from orchestration.dispatcher_handlers import read_wos_config  # noqa: PLC0415
    from orchestration.registry import Registry  # noqa: PLC0415
    from orchestration.wos_dashboard import _get_bisque_relay_base_url  # noqa: PLC0415  # private import intentional; try/except below bounds the risk

    config = read_wos_config()
    execution_enabled = bool(config.get("execution_enabled", False))
    status_label = "enabled" if execution_enabled else "stopped"

    registry = Registry()
    counts = registry.get_status_counts()
    active = counts.get("active", 0)
    ready = counts.get("ready-for-steward", 0)
    pending = counts.get("pending", 0)

    try:
        base_url = _get_bisque_relay_base_url()
        dashboard_url = f"{base_url}/files/wos-dashboard-active.html"
        link_line = f"Dashboard: {dashboard_url}"
    except Exception:
        link_line = "Dashboard: (URL unavailable)"

    lines = [
        f"WOS: {status_label}",
        f"Queue: {active} active, {ready} ready, {pending} pending",
        link_line,
    ]
    await message.reply_text("\n".join(lines))


async def _handle_restart(message: "Message", args: list[str]) -> None:
    if not args or args[0] not in ("dispatcher", "mcp", "all"):
        await message.reply_text("Usage: /restart dispatcher|mcp|all")
        return
    target = args[0]
    if target == "dispatcher":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Confirm", callback_data="confirm-restart-dispatcher"),
            InlineKeyboardButton("✗ Cancel", callback_data="cancel-restart"),
        ]])
        await message.reply_text(
            "Restart dispatcher? In-flight subagents may orphan. MCP and bot keep running.",
            reply_markup=keyboard,
        )
    elif target == "mcp":
        await message.reply_text(
            "⚠️ /restart mcp must be run from an external shell:\n"
            "  ~/lobster/scripts/restart-mcp.sh\n"
            "Running from Telegram invalidates the active MCP session immediately."
        )
    elif target == "all":
        await message.reply_text(
            "⚠️ /restart all is external-shell only. "
            "Full teardown must not be triggered from within the Telegram bot."
        )


# ---------------------------------------------------------------------------
# Callback query handlers (intercept before inbox write in handle_callback_query)
# ---------------------------------------------------------------------------

async def handle_todo_callback(query: "CallbackQuery") -> None:
    """Handle todo-done-*, todo-dismiss-*, todo-snooze-*-DATE callbacks."""
    from los.db import connect, mark_dismissed, mark_done, mark_snoozed  # noqa: PLC0415

    data = query.data  # e.g. "todo-done-42"
    parts = data.split("-")
    # parts: ["todo", action, item_id] or ["todo", "snooze", item_id, date]
    action = parts[1] if len(parts) > 1 else ""
    try:
        item_id = int(parts[2]) if len(parts) > 2 else -1
    except ValueError:
        await query.edit_message_text("Invalid callback data.")
        return

    conn = connect()
    try:
        if action == "done":
            mark_done(conn, item_id)
            conn.commit()
            await query.edit_message_text(f"✓ Marked done (#{item_id})")
        elif action == "dismiss":
            mark_dismissed(conn, item_id)
            conn.commit()
            await query.edit_message_text(f"Dismissed (#{item_id})")
        elif action == "snooze" and len(parts) > 3:
            until_date = parts[3]
            mark_snoozed(conn, item_id, until_date)
            conn.commit()
            await query.edit_message_text(f"Snoozed until {until_date} (#{item_id})")
        else:
            await query.edit_message_text("Unknown todo action.")
    finally:
        conn.close()


async def handle_restart_callback(query: "CallbackQuery") -> None:
    """Handle confirm-restart-dispatcher and cancel-restart callbacks."""
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    data = query.data
    if data == "cancel-restart":
        await query.edit_message_text("Restart cancelled.")
        return

    if data == "confirm-restart-dispatcher":
        # Write a restart_requested message to inbox; dispatcher handles it by exiting.
        inbox_dir = Path.home() / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        msg_id = f"{int(time.time() * 1000)}_restart"
        msg = {
            "id": msg_id,
            "source": "telegram",
            "type": "restart_requested",
            "chat_id": query.message.chat_id,
            "text": "[Restart dispatcher requested via Telegram]",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        (inbox_dir / f"{msg_id}.json").write_text(json.dumps(msg))
        await query.edit_message_text("Restarting dispatcher… will reconnect shortly.")
        return

    await query.edit_message_text("Unknown restart target.")
