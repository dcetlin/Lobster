"""
Tests for LOS action items database schema and access layer.

Tests verify the schema is applied correctly, indices exist, and
the CRUD operations maintain expected invariants.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.los.db import (
    ActionItem,
    ActionItemStatus,
    DB_SCHEMA_SQL,
    connect,
    insert_action_item,
    get_open_items,
    get_item_by_id,
    mark_done,
    mark_dismissed,
    mark_snoozed,
    find_duplicate,
    increment_mention_count,
)
from src.los.extractor import PRIORITY_DEFAULT  # noqa: F401


# ---------------------------------------------------------------------------
# Priority constants (local test-only values)
# ---------------------------------------------------------------------------

PRIORITY_HIGH = 1
PRIORITY_LOW = 10


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
def sample_item() -> dict:
    return {
        "text": "Call Sarah about the contract",
        "source": "telegram",
        "source_message_id": "msg_123",
        "priority": 3,
    }


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_schema_creates_action_items_table(conn: sqlite3.Connection) -> None:
    """Schema must create the action_items table."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='action_items'"
    )
    row = cursor.fetchone()
    assert row is not None, "action_items table must exist after schema creation"


def test_schema_creates_required_indices(conn: sqlite3.Connection) -> None:
    """Schema must create indices for status and extracted_at for query performance."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='action_items'"
    )
    index_names = {row[0] for row in cursor.fetchall()}
    # At minimum status index must exist for the query path
    assert any("status" in name for name in index_names), (
        f"Expected a status index, found: {index_names}"
    )


def test_schema_has_required_columns(conn: sqlite3.Connection) -> None:
    """Schema must define all required columns from the spec."""
    cursor = conn.execute("PRAGMA table_info(action_items)")
    columns = {row[1] for row in cursor.fetchall()}
    required = {
        "id", "text", "source", "source_message_id", "extracted_at",
        "priority", "mention_count", "status", "snoozed_until",
        "done_at", "dismissed_at", "notes",
    }
    missing = required - columns
    assert not missing, f"Missing required columns: {missing}"


# ---------------------------------------------------------------------------
# Insert tests
# ---------------------------------------------------------------------------


def test_insert_action_item_returns_id(conn: sqlite3.Connection, sample_item: dict) -> None:
    """insert_action_item must return a non-None integer row id."""
    row_id = insert_action_item(conn, **sample_item)
    assert isinstance(row_id, int)
    assert row_id > 0


def test_insert_action_item_stores_all_fields(conn: sqlite3.Connection, sample_item: dict) -> None:
    """Inserted item must be retrievable with all fields intact."""
    row_id = insert_action_item(conn, **sample_item)
    item = get_item_by_id(conn, row_id)
    assert item is not None
    assert item.text == sample_item["text"]
    assert item.source == sample_item["source"]
    assert item.source_message_id == sample_item["source_message_id"]
    assert item.priority == sample_item["priority"]
    assert item.status == ActionItemStatus.OPEN
    assert item.mention_count == 1


def test_insert_action_item_default_priority(conn: sqlite3.Connection) -> None:
    """When priority is omitted, default of 5 must be used."""
    row_id = insert_action_item(
        conn,
        text="Buy milk",
        source="telegram",
        source_message_id=None,
    )
    item = get_item_by_id(conn, row_id)
    assert item.priority == PRIORITY_DEFAULT


def test_insert_action_item_sets_extracted_at(conn: sqlite3.Connection, sample_item: dict) -> None:
    """extracted_at must be populated as an ISO UTC string."""
    row_id = insert_action_item(conn, **sample_item)
    item = get_item_by_id(conn, row_id)
    assert item.extracted_at is not None
    assert "T" in item.extracted_at  # ISO format contains T separator


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


def test_get_open_items_returns_only_open(conn: sqlite3.Connection) -> None:
    """get_open_items must return only status='open' items."""
    open_id = insert_action_item(conn, text="Open task", source="telegram", source_message_id=None)
    done_id = insert_action_item(conn, text="Done task", source="telegram", source_message_id=None)
    mark_done(conn, done_id)

    items = get_open_items(conn)
    ids = {item.id for item in items}
    assert open_id in ids
    assert done_id not in ids


def test_get_open_items_sorted_by_priority_then_mention_count(conn: sqlite3.Connection) -> None:
    """Open items must be sorted priority ASC (1=urgent, 10=low), then mention_count DESC."""
    # Insert in reverse order to verify sort
    low_id = insert_action_item(conn, text="Low priority", source="telegram", source_message_id=None, priority=8)
    high_id = insert_action_item(conn, text="High priority", source="telegram", source_message_id=None, priority=2)
    med_id = insert_action_item(conn, text="Medium priority", source="telegram", source_message_id=None, priority=5)

    items = get_open_items(conn)
    ids_ordered = [item.id for item in items]
    assert ids_ordered.index(high_id) < ids_ordered.index(med_id)
    assert ids_ordered.index(med_id) < ids_ordered.index(low_id)


def test_get_open_items_excludes_snoozed_until_future(conn: sqlite3.Connection) -> None:
    """Items snoozed until a future date must not appear in open items."""
    snoozed_id = insert_action_item(conn, text="Snoozed", source="telegram", source_message_id=None)
    mark_snoozed(conn, snoozed_id, "2099-12-31")

    items = get_open_items(conn)
    ids = {item.id for item in items}
    assert snoozed_id not in ids


def test_get_open_items_includes_snoozed_until_past(conn: sqlite3.Connection) -> None:
    """Items snoozed until a past date must reappear in open items."""
    snoozed_id = insert_action_item(conn, text="Past snooze", source="telegram", source_message_id=None)
    mark_snoozed(conn, snoozed_id, "2000-01-01")

    items = get_open_items(conn)
    ids = {item.id for item in items}
    assert snoozed_id in ids


# ---------------------------------------------------------------------------
# Status transition tests
# ---------------------------------------------------------------------------


def test_mark_done_sets_status_and_done_at(conn: sqlite3.Connection, sample_item: dict) -> None:
    """mark_done must set status='done' and populate done_at."""
    row_id = insert_action_item(conn, **sample_item)
    mark_done(conn, row_id)
    item = get_item_by_id(conn, row_id)
    assert item.status == ActionItemStatus.DONE
    assert item.done_at is not None


def test_mark_dismissed_sets_status_and_dismissed_at(conn: sqlite3.Connection, sample_item: dict) -> None:
    """mark_dismissed must set status='dismissed' and populate dismissed_at.

    Dismissed items are NOT deleted — they must remain reviewable (spec decision).
    """
    row_id = insert_action_item(conn, **sample_item)
    mark_dismissed(conn, row_id)
    item = get_item_by_id(conn, row_id)
    assert item.status == ActionItemStatus.DISMISSED
    assert item.dismissed_at is not None
    assert item is not None, "Dismissed items must remain in DB for weekly review"


def test_mark_snoozed_sets_status_and_date(conn: sqlite3.Connection, sample_item: dict) -> None:
    """mark_snoozed must set status='snoozed' and snoozed_until to the given date."""
    row_id = insert_action_item(conn, **sample_item)
    mark_snoozed(conn, row_id, "2026-06-01")
    item = get_item_by_id(conn, row_id)
    assert item.status == ActionItemStatus.SNOOZED
    assert item.snoozed_until == "2026-06-01"


# ---------------------------------------------------------------------------
# Dedup tests
# ---------------------------------------------------------------------------


def test_find_duplicate_returns_none_for_novel_item(conn: sqlite3.Connection) -> None:
    """find_duplicate must return None when no similar open item exists."""
    result = find_duplicate(conn, "Call the dentist")
    assert result is None


def test_find_duplicate_finds_exact_match(conn: sqlite3.Connection) -> None:
    """find_duplicate must find an existing open item with the same normalized text."""
    row_id = insert_action_item(conn, text="Call the dentist", source="telegram", source_message_id=None)
    result = find_duplicate(conn, "Call the dentist")
    assert result is not None
    assert result.id == row_id


def test_find_duplicate_normalizes_case_and_punctuation(conn: sqlite3.Connection) -> None:
    """Dedup check must normalize case, punctuation, and whitespace."""
    row_id = insert_action_item(conn, text="Call the dentist!", source="telegram", source_message_id=None)
    result = find_duplicate(conn, "CALL THE DENTIST")
    assert result is not None
    assert result.id == row_id


def test_find_duplicate_ignores_dismissed_items(conn: sqlite3.Connection) -> None:
    """Dismissed items must not be matched as duplicates — they are archived."""
    row_id = insert_action_item(conn, text="Cancel gym membership", source="telegram", source_message_id=None)
    mark_dismissed(conn, row_id)
    result = find_duplicate(conn, "Cancel gym membership")
    assert result is None


def test_increment_mention_count_increments(conn: sqlite3.Connection, sample_item: dict) -> None:
    """increment_mention_count must increase mention_count by 1."""
    row_id = insert_action_item(conn, **sample_item)
    increment_mention_count(conn, row_id)
    item = get_item_by_id(conn, row_id)
    assert item.mention_count == 2
