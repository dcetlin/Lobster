"""
Tests for LOS callback routing — todo-done-{id}, todo-snooze-{id}, todo-dismiss-{id}.

These callbacks are handled by route_los_callback, which returns the same shape
dict as route_callback_message in dispatcher_handlers.py (action, text, chat_id,
handled). The dispatcher integrates by calling route_los_callback before falling
through to WOS callbacks.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.los.db import connect, insert_action_item, get_item_by_id
from src.los.callbacks import route_los_callback

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
def item_id(conn: sqlite3.Connection) -> int:
    return insert_action_item(
        conn,
        text="Schedule dentist appointment",
        source="telegram",
        source_message_id="msg_99",
    )


# ---------------------------------------------------------------------------
# todo-done callback
# ---------------------------------------------------------------------------


def test_todo_done_callback_marks_item_done(conn: sqlite3.Connection, item_id: int) -> None:
    """todo-done-{id} must set item status to 'done'."""
    msg = {
        "callback_data": f"todo-done-{item_id}",
        "chat_id": 8075091586,
    }
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is True
    item = get_item_by_id(conn, item_id)
    assert item.status == "done"
    assert item.done_at is not None


def test_todo_done_callback_returns_confirmation_text(
    conn: sqlite3.Connection, item_id: int
) -> None:
    """todo-done callback must return a human-readable confirmation."""
    msg = {"callback_data": f"todo-done-{item_id}", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert "done" in result["text"].lower() or "complete" in result["text"].lower()


def test_todo_done_callback_unknown_id_returns_handled_false(
    conn: sqlite3.Connection,
) -> None:
    """todo-done with an unknown id must return handled=False."""
    msg = {"callback_data": "todo-done-999999", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is False


# ---------------------------------------------------------------------------
# todo-dismiss callback
# ---------------------------------------------------------------------------


def test_todo_dismiss_callback_marks_item_dismissed(
    conn: sqlite3.Connection, item_id: int
) -> None:
    """todo-dismiss-{id} must set item status to 'dismissed', not delete it."""
    msg = {"callback_data": f"todo-dismiss-{item_id}", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is True
    item = get_item_by_id(conn, item_id)
    assert item is not None, "Dismissed items must remain in DB"
    assert item.status == "dismissed"
    assert item.dismissed_at is not None


def test_todo_dismiss_callback_returns_confirmation_text(
    conn: sqlite3.Connection, item_id: int
) -> None:
    """todo-dismiss callback must return a human-readable confirmation."""
    msg = {"callback_data": f"todo-dismiss-{item_id}", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert result["text"]
    assert result["handled"] is True


# ---------------------------------------------------------------------------
# todo-snooze callback
# ---------------------------------------------------------------------------


def test_todo_snooze_callback_marks_item_snoozed(
    conn: sqlite3.Connection, item_id: int
) -> None:
    """todo-snooze-{id}-{date} must set status='snoozed' with the given date."""
    msg = {
        "callback_data": f"todo-snooze-{item_id}-2026-06-15",
        "chat_id": 8075091586,
    }
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is True
    item = get_item_by_id(conn, item_id)
    assert item.status == "snoozed"
    assert item.snoozed_until == "2026-06-15"


def test_todo_snooze_callback_invalid_date_returns_error(
    conn: sqlite3.Connection, item_id: int
) -> None:
    """todo-snooze with an invalid date must return handled=True with error text."""
    msg = {
        "callback_data": f"todo-snooze-{item_id}-not-a-date",
        "chat_id": 8075091586,
    }
    result = route_los_callback(msg, conn=conn)

    # The callback was recognized but the operation failed — it should inform the user
    assert "date" in result["text"].lower() or "invalid" in result["text"].lower() or "snooze" in result["text"].lower()


# ---------------------------------------------------------------------------
# Non-LOS callback pass-through
# ---------------------------------------------------------------------------


def test_non_los_callback_returns_handled_false(conn: sqlite3.Connection) -> None:
    """Callbacks that are not LOS patterns must return handled=False for pass-through."""
    msg = {"callback_data": "decide_retry:some-uow-id", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is False


def test_empty_callback_data_returns_handled_false(conn: sqlite3.Connection) -> None:
    """Empty callback_data must return handled=False."""
    msg = {"callback_data": "", "chat_id": 8075091586}
    result = route_los_callback(msg, conn=conn)

    assert result["handled"] is False
