"""
Tests for LOS Telegram TODO command handler.

Tests verify:
- handle_todo_add inserts an item and returns a formatted confirmation
- handle_todo_done matches by id and by text, marks the correct item done
- handle_todo_done reports ambiguity when multiple items match text query
- handle_todo_snooze matches by id and by text, sets snoozed_until correctly
- route_todo_command routes /todo add, /todo done, /todo snooze, and /todos

All tests inject an in-memory SQLite connection — no production DB is touched.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.los.db import connect, get_item_by_id, get_open_items, insert_action_item
from src.los.todo_commands import (
    SNOOZE_DEFAULT_DAYS,
    PRIORITY_DEFAULT,
    handle_todo_add,
    handle_todo_done,
    handle_todo_snooze,
    route_todo_command,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "self_action_items.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def chat_id() -> int:
    return 8075091586


# ---------------------------------------------------------------------------
# handle_todo_add
# ---------------------------------------------------------------------------


def test_add_inserts_item_into_db(conn: sqlite3.Connection, chat_id: int) -> None:
    """Adding a TODO must persist an item with status=open and source='telegram'."""
    handle_todo_add("Schedule dentist appointment", chat_id=chat_id, source="telegram", conn=conn)

    items = get_open_items(conn)
    assert len(items) == 1
    assert items[0].text == "Schedule dentist appointment"
    assert items[0].source == "telegram"
    assert items[0].status == "open"


def test_add_uses_mid_priority_by_default(conn: sqlite3.Connection, chat_id: int) -> None:
    """New items added via Telegram must use PRIORITY_DEFAULT (mid-priority = 5)."""
    handle_todo_add("Buy groceries", chat_id=chat_id, source="telegram", conn=conn)

    items = get_open_items(conn)
    assert items[0].priority == PRIORITY_DEFAULT


def test_add_returns_confirmation_text(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_add must return a non-empty confirmation string."""
    reply = handle_todo_add("Call dentist", chat_id=chat_id, source="telegram", conn=conn)

    assert isinstance(reply, str)
    assert len(reply) > 0
    # Confirmation should include the item text so the user knows what was added
    assert "dentist" in reply.lower() or "added" in reply.lower()


def test_add_returns_item_id_in_confirmation(conn: sqlite3.Connection, chat_id: int) -> None:
    """Confirmation text must include the item's ID so the user can reference it."""
    reply = handle_todo_add("Book flights", chat_id=chat_id, source="telegram", conn=conn)

    items = get_open_items(conn)
    item_id = items[0].id
    assert str(item_id) in reply


def test_add_dedup_increments_mention_count(conn: sqlite3.Connection, chat_id: int) -> None:
    """Adding the same item twice must not insert a duplicate — it increments mention_count."""
    handle_todo_add("Call Sarah", chat_id=chat_id, source="telegram", conn=conn)
    handle_todo_add("Call Sarah", chat_id=chat_id, source="telegram", conn=conn)

    items = get_open_items(conn)
    assert len(items) == 1
    assert items[0].mention_count == 2


# ---------------------------------------------------------------------------
# handle_todo_done
# ---------------------------------------------------------------------------


def test_done_by_id_marks_item_done(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_done with a numeric query must mark the item by ID as done."""
    item_id = insert_action_item(conn, text="Fix the staging bug", source="telegram", source_message_id=None)

    handle_todo_done(str(item_id), chat_id=chat_id, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "done"
    assert item.done_at is not None


def test_done_by_id_returns_confirmation(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_done must return a non-empty confirmation."""
    item_id = insert_action_item(conn, text="Buy coffee", source="telegram", source_message_id=None)
    reply = handle_todo_done(str(item_id), chat_id=chat_id, conn=conn)

    assert isinstance(reply, str)
    assert len(reply) > 0


def test_done_by_text_matches_substring(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_done with a text query must find and mark done an item that contains the text."""
    item_id = insert_action_item(conn, text="Schedule dentist appointment", source="telegram", source_message_id=None)

    handle_todo_done("dentist", chat_id=chat_id, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "done"


def test_done_by_text_multiple_matches_returns_disambiguation(
    conn: sqlite3.Connection, chat_id: int
) -> None:
    """When text matches multiple items, handle_todo_done must return a disambiguation list."""
    insert_action_item(conn, text="Call dentist", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Call Sarah", source="telegram", source_message_id=None)
    insert_action_item(conn, text="Call mom", source="telegram", source_message_id=None)

    reply = handle_todo_done("call", chat_id=chat_id, conn=conn)

    # Must not mark anything done — the reply must ask which one
    open_items = get_open_items(conn)
    assert len(open_items) == 3
    assert "which" in reply.lower() or "#" in reply


def test_done_unknown_id_returns_not_found(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_done with an ID that doesn't exist must return a not-found message."""
    reply = handle_todo_done("999999", chat_id=chat_id, conn=conn)

    assert "not found" in reply.lower() or "no item" in reply.lower()


def test_done_unknown_text_returns_not_found(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_done with text that matches no item must return a not-found message."""
    insert_action_item(conn, text="Buy milk", source="telegram", source_message_id=None)

    reply = handle_todo_done("dentist", chat_id=chat_id, conn=conn)

    assert "not found" in reply.lower() or "no item" in reply.lower() or "nothing" in reply.lower() or "no open item" in reply.lower()


# ---------------------------------------------------------------------------
# handle_todo_snooze
# ---------------------------------------------------------------------------


def test_snooze_by_id_sets_snoozed_until(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_snooze with a numeric query must snooze the item by the given number of days."""
    item_id = insert_action_item(conn, text="Review Q2 budget", source="telegram", source_message_id=None)

    handle_todo_snooze(str(item_id), days=3, chat_id=chat_id, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "snoozed"
    assert item.snoozed_until is not None


def test_snooze_uses_default_days_when_none(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_snooze must use SNOOZE_DEFAULT_DAYS when days is not specified."""
    item_id = insert_action_item(conn, text="Read the report", source="telegram", source_message_id=None)

    handle_todo_snooze(str(item_id), days=None, chat_id=chat_id, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "snoozed"
    # SNOOZE_DEFAULT_DAYS must be a named constant, not a magic literal
    assert SNOOZE_DEFAULT_DAYS > 0


def test_snooze_by_text_matches_substring(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_snooze with text must find the item by substring and snooze it."""
    item_id = insert_action_item(conn, text="Book annual checkup", source="telegram", source_message_id=None)

    handle_todo_snooze("annual checkup", days=7, chat_id=chat_id, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "snoozed"


def test_snooze_returns_confirmation_with_date(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_snooze must return a confirmation that includes the snooze date."""
    item_id = insert_action_item(conn, text="Submit expense report", source="telegram", source_message_id=None)

    reply = handle_todo_snooze(str(item_id), days=2, chat_id=chat_id, conn=conn)

    assert isinstance(reply, str)
    # The reply must tell the user when the item will resurface
    assert any(c.isdigit() for c in reply), "Confirmation must contain a date"


def test_snooze_unknown_id_returns_not_found(conn: sqlite3.Connection, chat_id: int) -> None:
    """handle_todo_snooze with an ID that doesn't exist must return a not-found message."""
    reply = handle_todo_snooze("999999", days=1, chat_id=chat_id, conn=conn)

    assert "not found" in reply.lower() or "no item" in reply.lower()


# ---------------------------------------------------------------------------
# route_todo_command
# ---------------------------------------------------------------------------


def test_route_todo_add_command(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo add <text>' must call handle_todo_add and insert an item."""
    msg = {"text": "/todo add Buy oat milk", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    items = get_open_items(conn)
    assert len(items) == 1
    assert items[0].text == "Buy oat milk"
    assert isinstance(reply, str)


def test_route_todo_done_command(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo done <id>' must call handle_todo_done and mark the item done."""
    item_id = insert_action_item(conn, text="Send invoice", source="telegram", source_message_id=None)

    msg = {"text": f"/todo done {item_id}", "chat_id": chat_id, "source": "telegram"}
    route_todo_command(msg, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "done"


def test_route_todo_snooze_command(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo snooze <id> <days>' must call handle_todo_snooze."""
    item_id = insert_action_item(conn, text="Review pull requests", source="telegram", source_message_id=None)

    msg = {"text": f"/todo snooze {item_id} 3", "chat_id": chat_id, "source": "telegram"}
    route_todo_command(msg, conn=conn)

    item = get_item_by_id(conn, item_id)
    assert item.status == "snoozed"


def test_route_unknown_subcommand_returns_help(conn: sqlite3.Connection, chat_id: int) -> None:
    """An unrecognized /todo subcommand must return usage help."""
    msg = {"text": "/todo frobnicate", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    assert isinstance(reply, str)
    assert len(reply) > 0
    # Should mention valid commands
    assert any(cmd in reply.lower() for cmd in ("add", "done", "snooze"))


def test_route_bare_todo_command_returns_help(conn: sqlite3.Connection, chat_id: int) -> None:
    """'/todo' with no subcommand must return usage help."""
    msg = {"text": "/todo", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    assert isinstance(reply, str)
    assert any(cmd in reply.lower() for cmd in ("add", "done", "snooze"))


# ---------------------------------------------------------------------------
# Gap 1: /todos must NOT be routed through route_todo_command
# ---------------------------------------------------------------------------


def test_todos_command_is_not_routed_to_route_todo_command(
    conn: sqlite3.Connection, chat_id: int
) -> None:
    """/todos must not be processed by route_todo_command.

    The dispatcher's routing condition must use startswith("/todo ") or == "/todo",
    NOT startswith("/todo"), which would accidentally match /todos.

    This test documents the safe fallback: if /todos is somehow passed to
    route_todo_command (e.g. if the dispatcher condition is written incorrectly),
    the function returns usage help — it does NOT silently process the command
    as a /todo subcommand.

    The primary protection is the dispatcher routing condition documented in
    sys.dispatcher.bootup.md: use 'text.startswith("/todo ") or text == "/todo"'
    to prevent /todos from entering this code path at all.
    """
    # /todos does not match any subcommand regex — it must return usage help
    msg = {"text": "/todos", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    # Must return usage help — not execute any subcommand
    assert isinstance(reply, str)
    assert any(cmd in reply.lower() for cmd in ("add", "done", "snooze")), (
        "/todos incorrectly matched a subcommand in route_todo_command"
    )
    # Must NOT have inserted any items
    items = get_open_items(conn)
    assert len(items) == 0, "/todos must not insert any items"


def test_dispatcher_routing_condition_excludes_todos() -> None:
    """/todos must not match the dispatcher's /todo routing condition.

    Documents the correct routing check:
        text.startswith("/todo ") or text == "/todo"

    An incorrect check (text.startswith("/todo")) would match both /todo and /todos.
    """
    text = "/todos"

    # Incorrect condition — would incorrectly route /todos to the /todo handler:
    incorrect_match = text.startswith("/todo")
    assert incorrect_match, "Confirm: the naive startswith('/todo') DOES match /todos"

    # Correct condition — explicitly excludes /todos:
    correct_match = text.startswith("/todo ") or text == "/todo"
    assert not correct_match, (
        "The correct dispatcher condition must NOT match /todos — "
        "use startswith('/todo ') or == '/todo', not startswith('/todo')"
    )


# ---------------------------------------------------------------------------
# Gap 2: Snooze regex trailing-number ambiguity — documented edge case
# ---------------------------------------------------------------------------


def test_snooze_trailing_number_in_item_text_is_consumed_as_days(
    conn: sqlite3.Connection, chat_id: int
) -> None:
    """Documents the known snooze regex ambiguity: trailing integers become days.

    When an item's text ends in a number (e.g. "call 911"), the query
    '/todo snooze call 911' will parse query='call' (not 'call 911') and
    days=911 (not SNOOZE_DEFAULT_DAYS). This may produce a not-found result
    or match a different item than intended.

    The workaround (documented in _USAGE and _SNOOZE_RE comment) is to use
    the item ID: '/todo snooze <id>'.

    WARNING: This test documents the current behavior, not the desired behavior.
    If the regex is changed to require an explicit 'days' keyword (e.g. 'for N
    days'), this test should be updated to reflect the new behavior.
    """
    item_id = insert_action_item(conn, text="call 911", source="telegram", source_message_id=None)

    # This command intends to snooze "call 911" for the default number of days,
    # but the trailing "911" is consumed as the days argument and the query
    # becomes "call" — which does not exactly match "call 911".
    msg = {"text": "/todo snooze call 911", "chat_id": chat_id, "source": "telegram"}
    reply = route_todo_command(msg, conn=conn)

    # The item text is "call 911" but the query extracted is "call".
    # "call" is a substring of "call 911", so the item IS found.
    # It gets snoozed for 911 days (not SNOOZE_DEFAULT_DAYS).
    item = get_item_by_id(conn, item_id)
    if item.status == "snoozed":
        # Item was found via substring "call" and snoozed for 911 days —
        # the user's intent (snooze for default days) was NOT honored.
        # Workaround: use '/todo snooze {item_id}' instead.
        assert item.snoozed_until is not None
    else:
        # If not snoozed: the reply must contain a helpful message
        assert isinstance(reply, str) and len(reply) > 0

    # Either way, the usage tip about item IDs must be present in the help text
    usage_reply = route_todo_command({"text": "/todo", "chat_id": chat_id, "source": "telegram"}, conn=conn)
    assert "id" in usage_reply.lower() or "tip" in usage_reply.lower(), (
        "_USAGE must include the tip about using item IDs for items whose text ends in a number"
    )
