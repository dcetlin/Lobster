"""
LOS — Telegram Inline Button Callback Routing

Handles inline keyboard button presses for the /todos interface:
    todo-done-{id}              → mark item done
    todo-dismiss-{id}           → archive item (status='dismissed', not deleted)
    todo-snooze-{id}-{date}     → snooze until custom date (YYYY-MM-DD)

Returns the same shape dict as route_callback_message in dispatcher_handlers.py:
    {"action": "send_reply", "text": ..., "chat_id": ..., "handled": bool}

Dispatcher integration:
    from src.los.callbacks import route_los_callback

    if msg.get("type") == "callback":
        los_result = route_los_callback(msg, conn=conn)
        if los_result["handled"]:
            # send reply and mark processed
        else:
            result = route_callback_message(msg, registry=registry)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .db import (
    connect,
    get_item_by_id,
    mark_dismissed,
    mark_done,
    mark_snoozed,
    DEFAULT_DB_PATH,
)

log = logging.getLogger(__name__)

# Regex patterns for recognized callback data
_DONE_PATTERN = re.compile(r"^todo-done-(\d+)$")
_DISMISS_PATTERN = re.compile(r"^todo-dismiss-(\d+)$")
_SNOOZE_PATTERN = re.compile(r"^todo-snooze-(\d+)-(.+)$")

# ISO date validation (simple — YYYY-MM-DD)
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _result(text: str, chat_id: Any, handled: bool) -> dict:
    return {"action": "send_reply", "text": text, "chat_id": chat_id, "handled": handled}


def _handle_done(conn: sqlite3.Connection, item_id: int, chat_id: Any) -> dict:
    item = get_item_by_id(conn, item_id)
    if item is None:
        return _result(f"Item #{item_id} not found.", chat_id, handled=False)
    mark_done(conn, item_id)
    return _result(
        f"Done: \"{item.text[:60]}\"",
        chat_id,
        handled=True,
    )


def _handle_dismiss(conn: sqlite3.Connection, item_id: int, chat_id: Any) -> dict:
    item = get_item_by_id(conn, item_id)
    if item is None:
        return _result(f"Item #{item_id} not found.", chat_id, handled=False)
    mark_dismissed(conn, item_id)
    return _result(
        f"Dismissed: \"{item.text[:60]}\"",
        chat_id,
        handled=True,
    )


def _handle_snooze(
    conn: sqlite3.Connection, item_id: int, date_str: str, chat_id: Any
) -> dict:
    if not _DATE_PATTERN.match(date_str):
        return _result(
            f"Invalid snooze date \"{date_str}\". Use YYYY-MM-DD format.",
            chat_id,
            handled=True,
        )
    item = get_item_by_id(conn, item_id)
    if item is None:
        return _result(f"Item #{item_id} not found.", chat_id, handled=False)
    mark_snoozed(conn, item_id, date_str)
    return _result(
        f"Snoozed until {date_str}: \"{item.text[:60]}\"",
        chat_id,
        handled=True,
    )


def route_los_callback(
    msg: dict,
    *,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Route a Telegram callback_data to the appropriate LOS handler.

    Args:
        msg:     Raw inbox message dict with 'callback_data' and 'chat_id'.
        conn:    Optional open DB connection (for testing — avoids opening production DB).
        db_path: Optional path override for production DB (defaults to DEFAULT_DB_PATH).

    Returns:
        dict with keys: action, text, chat_id, handled.
        handled=True  → this callback was handled; dispatcher must send reply.
        handled=False → not a LOS callback; dispatcher should fall through.
    """
    data: str = msg.get("callback_data", "")
    chat_id = msg.get("chat_id")

    if not data:
        return _result("", chat_id, handled=False)

    # Lazy-open connection if not injected (production path)
    _own_conn = False
    if conn is None:
        conn = connect(db_path or DEFAULT_DB_PATH)
        _own_conn = True

    try:
        # --- todo-done-{id} ---
        m = _DONE_PATTERN.match(data)
        if m:
            return _handle_done(conn, int(m.group(1)), chat_id)

        # --- todo-dismiss-{id} ---
        m = _DISMISS_PATTERN.match(data)
        if m:
            return _handle_dismiss(conn, int(m.group(1)), chat_id)

        # --- todo-snooze-{id}-{date} ---
        m = _SNOOZE_PATTERN.match(data)
        if m:
            return _handle_snooze(conn, int(m.group(1)), m.group(2), chat_id)

        # Not a LOS callback — pass through
        return _result("", chat_id, handled=False)

    finally:
        if _own_conn:
            conn.close()
