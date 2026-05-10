"""
LOS — Telegram TODO Command Handler

Routes /todo subcommands and natural-language patterns to the action items DB.
This is the dispatcher entry point for all TODO mutations coming through Telegram.

Supported command forms:
    /todo add <text>      — insert item (source='telegram', priority=5)
    /todo done <query>    — mark item done (query = id or partial text)
    /todo snooze <query> [days]  — snooze item for N days (default: SNOOZE_DEFAULT_DAYS)
    /todo                 — returns usage help

Dispatcher integration:
    from src.los.todo_commands import route_todo_command

    if msg.get("text", "").lower().startswith("/todo"):
        reply = route_todo_command(msg)
        send_reply(chat_id=chat_id, text=reply, source=source)

Design principles:
- Pure functions where possible — conn is injected, not global
- Named constants for every spec-derived value (priority, default days)
- Item matching is deterministic: numeric query = ID lookup, text = substring
- Disambiguation is always explicit — never silently mark the wrong item
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .db import (
    ActionItemStatus,
    DEFAULT_DB_PATH,
    ActionItem,
    connect,
    find_duplicate,
    get_item_by_id,
    get_open_items,
    increment_mention_count,
    insert_action_item,
    mark_done,
    mark_snoozed,
)

# ---------------------------------------------------------------------------
# Named constants (derived from spec — never use magic literals)
# ---------------------------------------------------------------------------

PRIORITY_DEFAULT = 5        # mid-priority, per design doc §1
SNOOZE_DEFAULT_DAYS = 3     # default when user omits the days argument

# Number of match candidates to show in disambiguation replies
DISAMBIGUATION_LIMIT = 5

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_numeric(s: str) -> bool:
    return s.strip().lstrip("-").isdigit()


def _today_plus(days: int) -> str:
    """Return ISO-8601 date string for today + N days."""
    return (date.today() + timedelta(days=days)).isoformat()


def _format_item_line(item: ActionItem) -> str:
    """Single-line summary for disambiguation lists."""
    snippet = item.text[:50] + ("..." if len(item.text) > 50 else "")
    return f"#{item.id}: {snippet}"


def _find_by_query(
    conn: sqlite3.Connection,
    query: str,
) -> tuple[Optional[ActionItem], list[ActionItem], str]:
    """Resolve a user-supplied query to an ActionItem.

    Returns:
        (item, [], "")           — exactly one match
        (None, candidates, "")  — multiple matches (disambiguation needed)
        (None, [], reason)      — no match; reason is a human-readable message

    Only considers open and snoozed items — done/dismissed are excluded from
    matching because users should not be able to re-mutate closed items.
    """
    query = query.strip()
    if _is_numeric(query):
        item_id = int(query)
        item = get_item_by_id(conn, item_id)
        if item is None:
            return None, [], f"No item found with ID #{item_id}."
        if item.status in (ActionItemStatus.DONE, ActionItemStatus.DISMISSED):
            return None, [], f"Item #{item_id} is already {item.status}."
        return item, [], ""

    # Text search — substring match against open/snoozed items
    lower_query = query.lower()
    all_open = get_open_items(conn, limit=200)
    candidates = [
        item for item in all_open
        if lower_query in item.text.lower()
    ]

    if not candidates:
        return None, [], f"No open item matching \"{query}\"."
    if len(candidates) == 1:
        return candidates[0], [], ""
    # Multiple matches — caller must handle disambiguation
    return None, candidates[:DISAMBIGUATION_LIMIT], ""


def _disambiguation_reply(candidates: list[ActionItem], action: str) -> str:
    """Format a disambiguation reply listing matching items."""
    lines = [f"Multiple items match. Which one do you want to {action}?"]
    lines.extend(_format_item_line(item) for item in candidates)
    lines.append(f"\nReply with /todo {action} <id>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers (pure: return reply text, side-effect DB writes at boundary)
# ---------------------------------------------------------------------------


def handle_todo_add(
    text: str,
    *,
    chat_id: int,
    source: str = "telegram",
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Insert a new action item. Returns a formatted confirmation.

    If the item already exists (same normalized text), increments mention_count
    instead of inserting a duplicate.
    """
    _own_conn = False
    if conn is None:
        conn = connect(db_path or DEFAULT_DB_PATH)
        _own_conn = True

    try:
        existing = find_duplicate(conn, text)
        if existing is not None:
            increment_mention_count(conn, existing.id)
            return (
                f"Already on your list (#{existing.id}): \"{existing.text[:60]}\"\n"
                f"Mention count bumped to {existing.mention_count + 1}."
            )

        item_id = insert_action_item(
            conn,
            text=text,
            source=source,
            source_message_id=None,
            priority=PRIORITY_DEFAULT,
        )
        return f"Added #{item_id}: \"{text[:60]}\""
    finally:
        if _own_conn:
            conn.close()


def handle_todo_done(
    query: str,
    *,
    chat_id: int,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Mark a TODO item as done. Returns a formatted confirmation or error.

    query: item ID (numeric) or partial text for substring match.
    """
    _own_conn = False
    if conn is None:
        conn = connect(db_path or DEFAULT_DB_PATH)
        _own_conn = True

    try:
        item, candidates, reason = _find_by_query(conn, query)
        if reason:
            return reason
        if candidates:
            return _disambiguation_reply(candidates, "done")

        mark_done(conn, item.id)
        return f"Done: \"{item.text[:60]}\" (#{item.id})"
    finally:
        if _own_conn:
            conn.close()


def handle_todo_snooze(
    query: str,
    *,
    days: Optional[int],
    chat_id: int,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Snooze a TODO item for N days. Returns a formatted confirmation or error.

    query: item ID (numeric) or partial text.
    days:  number of days to snooze; defaults to SNOOZE_DEFAULT_DAYS if None.
    """
    snooze_days = days if days is not None else SNOOZE_DEFAULT_DAYS
    until_date = _today_plus(snooze_days)

    _own_conn = False
    if conn is None:
        conn = connect(db_path or DEFAULT_DB_PATH)
        _own_conn = True

    try:
        item, candidates, reason = _find_by_query(conn, query)
        if reason:
            return reason
        if candidates:
            return _disambiguation_reply(candidates, "snooze")

        mark_snoozed(conn, item.id, until_date)
        return (
            f"Snoozed until {until_date}: \"{item.text[:60]}\" (#{item.id})"
        )
    finally:
        if _own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Command parser (pure — no DB access)
# ---------------------------------------------------------------------------

# /todo add <text>
_ADD_RE = re.compile(r"^/todo\s+add\s+(.+)$", re.IGNORECASE)

# /todo done <query>
_DONE_RE = re.compile(r"^/todo\s+done\s+(.+)$", re.IGNORECASE)

# /todo snooze <query> [<days>]
# Days are optional — if omitted, defaults to SNOOZE_DEFAULT_DAYS.
#
# AMBIGUITY: A trailing integer is always parsed as the days argument.
# If an item's text ends in a number (e.g. "call 911"), a query like
# "/todo snooze call 911" will parse query='call' and days=911, not
# query='call 911' and days=None.
# Use the item ID to avoid this: "/todo snooze <id>" for items whose text ends in a number.
_SNOOZE_RE = re.compile(r"^/todo\s+snooze\s+(.+?)(?:\s+(\d+))?\s*$", re.IGNORECASE)

_USAGE = (
    "Usage:\n"
    "  /todo add <text>          — add item to your list\n"
    "  /todo done <id or text>   — mark item done\n"
    "  /todo snooze <id or text> [days]  — snooze item (default: "
    f"{SNOOZE_DEFAULT_DAYS} days)\n"
    "  /todos                    — show current open items\n"
    "\n"
    "Tip: for items whose text ends in a number, use the item ID\n"
    "  (e.g. '/todo snooze 42' instead of '/todo snooze call 911')."
)


def route_todo_command(
    msg: dict,
    *,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Dispatcher entry point. Routes /todo subcommands to the correct handler.

    Args:
        msg:     Raw inbox message dict with 'text', 'chat_id', 'source'.
        conn:    Optional DB connection (injected in tests; None = open production DB).
        db_path: Optional DB path override (passed through to handlers).

    Returns:
        Reply text string. The dispatcher is responsible for calling send_reply.
    """
    text: str = (msg.get("text") or "").strip()
    chat_id: int = msg.get("chat_id", 0)
    source: str = msg.get("source", "telegram")

    m = _ADD_RE.match(text)
    if m:
        return handle_todo_add(
            m.group(1).strip(),
            chat_id=chat_id,
            source=source,
            conn=conn,
            db_path=db_path,
        )

    m = _DONE_RE.match(text)
    if m:
        return handle_todo_done(
            m.group(1).strip(),
            chat_id=chat_id,
            conn=conn,
            db_path=db_path,
        )

    m = _SNOOZE_RE.match(text)
    if m:
        query = m.group(1).strip()
        days = int(m.group(2)) if m.group(2) else None
        return handle_todo_snooze(
            query,
            days=days,
            chat_id=chat_id,
            conn=conn,
            db_path=db_path,
        )

    return _USAGE
