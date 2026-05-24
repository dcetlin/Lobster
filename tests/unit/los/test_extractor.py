"""
Tests for LOS action item extractor.

Tests verify:
- parse_llm_response correctly validates and normalizes subagent JSON output
- extract_action_items persists items to the DB
- Dedup logic fires when a duplicate is found
- Empty item lists are handled gracefully

No Anthropic API calls occur anywhere in this module — extraction is
performed by the calling subagent, not by extractor.py.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.los.db import connect, get_open_items, get_item_by_id, find_duplicate, compute_dedup_key
from src.los.extractor import (
    extract_action_items,
    parse_llm_response,
    PRIORITY_MIN,
    PRIORITY_MAX,
    PRIORITY_DEFAULT,
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


# ---------------------------------------------------------------------------
# Unit tests for compute_dedup_key (lives in db — tested here for
# convenience since test_extractor covers the full extraction flow)
# ---------------------------------------------------------------------------


def test_compute_dedup_key_is_deterministic() -> None:
    """compute_dedup_key must return the same value for the same input."""
    key1 = compute_dedup_key("Call Sarah about the contract")
    key2 = compute_dedup_key("Call Sarah about the contract")
    assert key1 == key2


def test_compute_dedup_key_normalizes_case() -> None:
    """compute_dedup_key must normalize case."""
    assert compute_dedup_key("CALL SARAH") == compute_dedup_key("call sarah")


def test_compute_dedup_key_normalizes_punctuation() -> None:
    """compute_dedup_key must strip punctuation."""
    assert compute_dedup_key("Call Sarah!") == compute_dedup_key("Call Sarah")


def test_compute_dedup_key_collapses_whitespace() -> None:
    """compute_dedup_key must collapse extra whitespace."""
    assert compute_dedup_key("Call  Sarah") == compute_dedup_key("Call Sarah")


def test_compute_dedup_key_returns_16_char_hex() -> None:
    """compute_dedup_key must return a 16-character hex string."""
    key = compute_dedup_key("some text")
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


# ---------------------------------------------------------------------------
# Unit tests for parse_llm_response — pure function, no DB
# ---------------------------------------------------------------------------


class TestParseLlmResponse:
    """Tests for parse_llm_response — pure function, no DB or API calls."""

    def test_parses_valid_json_list(self) -> None:
        raw = json.dumps([
            {"text": "Call Sarah", "priority": 3},
            {"text": "Buy groceries", "priority": 7},
        ])
        items = parse_llm_response(raw)
        assert len(items) == 2
        assert items[0]["text"] == "Call Sarah"
        assert items[0]["priority"] == 3

    def test_empty_list_returns_empty(self) -> None:
        raw = "[]"
        items = parse_llm_response(raw)
        assert items == []

    def test_invalid_json_returns_empty(self) -> None:
        """When the subagent returns malformed JSON, parser must return empty list."""
        items = parse_llm_response("not json at all")
        assert items == []

    def test_non_list_json_returns_empty(self) -> None:
        """When the subagent returns a dict instead of a list, must return empty."""
        raw = json.dumps({"text": "something"})
        items = parse_llm_response(raw)
        assert items == []

    def test_missing_text_field_skipped(self) -> None:
        """Items without a 'text' field must be skipped."""
        raw = json.dumps([
            {"priority": 3},  # missing text
            {"text": "Valid item", "priority": 5},
        ])
        items = parse_llm_response(raw)
        assert len(items) == 1
        assert items[0]["text"] == "Valid item"

    def test_priority_clamped_to_valid_range(self) -> None:
        """Priority must be clamped to [PRIORITY_MIN, PRIORITY_MAX]."""
        raw = json.dumps([
            {"text": "Too urgent", "priority": -5},
            {"text": "Too low", "priority": 100},
        ])
        items = parse_llm_response(raw)
        assert items[0]["priority"] == PRIORITY_MIN
        assert items[1]["priority"] == PRIORITY_MAX

    def test_missing_priority_defaults_to_five(self) -> None:
        """When priority is absent, default to PRIORITY_DEFAULT."""
        raw = json.dumps([{"text": "Do something"}])
        items = parse_llm_response(raw)
        assert items[0]["priority"] == PRIORITY_DEFAULT


# ---------------------------------------------------------------------------
# Tests for extract_action_items — DB persistence, no API calls
# ---------------------------------------------------------------------------


class TestExtractActionItems:
    """Tests for extract_action_items — verifies DB persistence behavior.

    The caller (a subagent) provides pre-extracted items; extract_action_items
    handles only dedup checking and DB writes.
    """

    def test_writes_provided_items_to_db(self, conn: sqlite3.Connection) -> None:
        """Provided items must be persisted to the action_items table."""
        items = [
            {"text": "Call dentist", "priority": 3},
            {"text": "Renew passport", "priority": 6},
        ]

        result = extract_action_items(
            conn=conn,
            items=items,
            source="telegram",
            source_message_id="msg_002",
        )

        assert len(result) == 2
        open_items = get_open_items(conn)
        texts = {item.text for item in open_items}
        assert "Call dentist" in texts
        assert "Renew passport" in texts

    def test_dedup_increments_mention_count_instead_of_inserting(
        self, conn: sqlite3.Connection
    ) -> None:
        """When an item matches an existing open item, mention_count must increment."""
        from src.los.db import insert_action_item

        existing_id = insert_action_item(
            conn,
            text="Call dentist",
            source="telegram",
            source_message_id="msg_first",
        )

        extract_action_items(
            conn=conn,
            items=[{"text": "Call dentist", "priority": 3}],
            source="telegram",
            source_message_id="msg_003",
        )

        item = get_item_by_id(conn, existing_id)
        assert item.mention_count == 2

        # Only one item in DB (no duplicate inserted)
        all_open = get_open_items(conn)
        dentist_items = [i for i in all_open if i.text == "Call dentist"]
        assert len(dentist_items) == 1

    def test_empty_items_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        """When items list is empty, extract_action_items must return []."""
        result = extract_action_items(
            conn=conn,
            items=[],
            source="telegram",
            source_message_id="msg_004",
        )

        assert result == []
        assert get_open_items(conn) == []

    def test_source_and_message_id_stored_correctly(self, conn: sqlite3.Connection) -> None:
        """Source and source_message_id must be stored in the inserted row."""
        result = extract_action_items(
            conn=conn,
            items=[{"text": "Fix the leak", "priority": 2}],
            source="voice_note",
            source_message_id="vn_007",
        )

        assert len(result) == 1
        open_items = get_open_items(conn)
        assert open_items[0].source == "voice_note"
        assert open_items[0].source_message_id == "vn_007"
