"""
Tests for LOS action item extractor.

Tests verify:
- The extractor correctly calls the LLM with the right prompt
- It handles the response correctly, mapping fields to ActionItem-like dicts
- Dedup logic fires when a duplicate is found
- The extractor writes to the DB (side-effectful boundary)
- Empty LLM responses are handled gracefully

All Anthropic API calls are mocked — no production hits in unit tests.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.los.db import connect, get_open_items, get_item_by_id, find_duplicate
from src.los.extractor import (
    extract_action_items,
    parse_llm_response,
    compute_dedup_key,
    EXTRACTION_MODEL,
)


# ---------------------------------------------------------------------------
# Constants from spec
# ---------------------------------------------------------------------------

EXTRACTION_MODEL_EXPECTED = "claude-haiku-4-5"


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


def _make_anthropic_response(items: list[dict]) -> MagicMock:
    """Build a mock Anthropic response with the given items as JSON content."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps(items)
    return mock_response


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------


def test_extraction_model_is_haiku() -> None:
    """Extractor must use claude-haiku-4-5 for cost efficiency (spec requirement)."""
    assert EXTRACTION_MODEL == EXTRACTION_MODEL_EXPECTED


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
        """When the LLM returns malformed JSON, extractor must return empty list."""
        items = parse_llm_response("not json at all")
        assert items == []

    def test_non_list_json_returns_empty(self) -> None:
        """When the LLM returns a dict instead of a list, must return empty."""
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
        """Priority must be clamped to [1, 10]."""
        raw = json.dumps([
            {"text": "Too urgent", "priority": -5},
            {"text": "Too low", "priority": 100},
        ])
        items = parse_llm_response(raw)
        assert items[0]["priority"] == 1
        assert items[1]["priority"] == 10

    def test_missing_priority_defaults_to_5(self) -> None:
        """When priority is absent, default to 5."""
        raw = json.dumps([{"text": "Do something"}])
        items = parse_llm_response(raw)
        assert items[0]["priority"] == 5


# ---------------------------------------------------------------------------
# Integration tests for extract_action_items (mocked Claude)
# ---------------------------------------------------------------------------


class TestExtractActionItems:
    """Tests for extract_action_items — end-to-end with mocked Anthropic client."""

    def test_calls_anthropic_with_haiku_model(self, conn: sqlite3.Connection) -> None:
        """extract_action_items must call the Anthropic API with claude-haiku-4-5."""
        llm_items = [{"text": "Follow up with the bank", "priority": 4}]
        mock_response = _make_anthropic_response(llm_items)

        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.return_value = mock_response

            extract_action_items(
                conn=conn,
                text="I need to follow up with the bank this week.",
                source="telegram",
                source_message_id="msg_001",
            )

            call_kwargs = instance.messages.create.call_args[1]
            assert call_kwargs["model"] == EXTRACTION_MODEL_EXPECTED

    def test_writes_extracted_items_to_db(self, conn: sqlite3.Connection) -> None:
        """Extracted items must be persisted to the action_items table."""
        llm_items = [
            {"text": "Call dentist", "priority": 3},
            {"text": "Renew passport", "priority": 6},
        ]
        mock_response = _make_anthropic_response(llm_items)

        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.return_value = mock_response

            result = extract_action_items(
                conn=conn,
                text="I need to call the dentist and renew my passport.",
                source="telegram",
                source_message_id="msg_002",
            )

        assert len(result) == 2
        items = get_open_items(conn)
        texts = {item.text for item in items}
        assert "Call dentist" in texts
        assert "Renew passport" in texts

    def test_dedup_increments_mention_count_instead_of_inserting(
        self, conn: sqlite3.Connection
    ) -> None:
        """When an extracted item matches an existing open item, mention_count must increment."""
        from src.los.db import insert_action_item

        existing_id = insert_action_item(
            conn,
            text="Call dentist",
            source="telegram",
            source_message_id="msg_first",
        )

        llm_items = [{"text": "Call dentist", "priority": 3}]
        mock_response = _make_anthropic_response(llm_items)

        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.return_value = mock_response

            extract_action_items(
                conn=conn,
                text="I still need to call the dentist.",
                source="telegram",
                source_message_id="msg_003",
            )

        item = get_item_by_id(conn, existing_id)
        assert item.mention_count == 2

        # Only one item in DB (no duplicate inserted)
        all_open = get_open_items(conn)
        dentist_items = [i for i in all_open if i.text == "Call dentist"]
        assert len(dentist_items) == 1

    def test_empty_llm_response_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        """When LLM finds no action items, extract_action_items must return []."""
        mock_response = _make_anthropic_response([])

        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.return_value = mock_response

            result = extract_action_items(
                conn=conn,
                text="Nice day today!",
                source="telegram",
                source_message_id="msg_004",
            )

        assert result == []
        items = get_open_items(conn)
        assert len(items) == 0

    def test_llm_failure_returns_empty_list_without_raising(
        self, conn: sqlite3.Connection
    ) -> None:
        """When the Anthropic API call fails, extract_action_items must return []
        rather than propagating the exception to the caller."""
        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.side_effect = Exception("API unavailable")

            result = extract_action_items(
                conn=conn,
                text="I need to call Sarah.",
                source="telegram",
                source_message_id="msg_005",
            )

        assert result == []

    def test_source_and_message_id_stored_correctly(self, conn: sqlite3.Connection) -> None:
        """Source and source_message_id must be stored in the inserted row."""
        llm_items = [{"text": "Fix the leak", "priority": 2}]
        mock_response = _make_anthropic_response(llm_items)

        with patch("src.los.extractor.anthropic.Anthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create.return_value = mock_response

            result = extract_action_items(
                conn=conn,
                text="I need to fix the leak in the bathroom.",
                source="voice_note",
                source_message_id="vn_007",
            )

        assert len(result) == 1
        items = get_open_items(conn)
        assert items[0].source == "voice_note"
        assert items[0].source_message_id == "vn_007"
