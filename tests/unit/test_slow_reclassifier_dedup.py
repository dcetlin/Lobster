"""
Tests for the _recent_duplicate_exists deduplication gate in slow_reclassifier.

Covers:
- Gate returns True when a recent identical pattern_observation exists
- Gate returns False when no entry exists
- Gate returns False when existing entry is older than 12 hours
- Gate returns False when existing entry has a different pattern_type

WOS-UoW: uow_20260522_121905
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from src.classifiers.slow_reclassifier import (
    PatternObservation,
    _recent_duplicate_exists,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_inmemory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_events_table(conn: sqlite3.Connection, events: list[dict]) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
    """)
    for ev in events:
        conn.execute(
            "INSERT INTO events (timestamp, type, source, content, metadata) VALUES (?, ?, ?, ?, ?)",
            (
                ev["timestamp"],
                ev.get("type", "user_message"),
                ev.get("source", "chat-123"),
                ev.get("content", "test content"),
                json.dumps(ev.get("metadata", {})),
            ),
        )
    conn.commit()


def _make_obs(pattern_type: str = "design_session", source: str = "chat-123") -> PatternObservation:
    return PatternObservation(
        pattern_type=pattern_type,
        source=source,
        event_ids=[1, 2, 3],
        signal_type="design_session",
        urgency="normal",
        posture_hint="structural_coherence",
    )


def _recent_ts(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecentDuplicateExists:

    def test_dedup_gate_skips_when_recent_identical_exists(self):
        """Returns True when a pattern_observation with same source+pattern_type exists within 12 hours."""
        conn = _open_inmemory_db()
        _seed_events_table(conn, [
            {
                "timestamp": _recent_ts(hours_ago=1),
                "type": "pattern_observation",
                "source": "chat-123",
                "metadata": {"pattern_type": "design_session"},
            }
        ])
        obs = _make_obs(pattern_type="design_session", source="chat-123")
        assert _recent_duplicate_exists(conn, obs) is True

    def test_dedup_gate_allows_when_no_recent_entry(self):
        """Returns False when no pattern_observation exists for the source+pattern_type."""
        conn = _open_inmemory_db()
        _seed_events_table(conn, [])
        obs = _make_obs(pattern_type="design_session", source="chat-123")
        assert _recent_duplicate_exists(conn, obs) is False

    def test_dedup_gate_allows_when_entry_is_old(self):
        """Returns False when existing entry is older than 12 hours."""
        conn = _open_inmemory_db()
        _seed_events_table(conn, [
            {
                "timestamp": _recent_ts(hours_ago=13),
                "type": "pattern_observation",
                "source": "chat-123",
                "metadata": {"pattern_type": "design_session"},
            }
        ])
        obs = _make_obs(pattern_type="design_session", source="chat-123")
        assert _recent_duplicate_exists(conn, obs) is False

    def test_dedup_gate_allows_different_pattern_type(self):
        """Returns False when existing entry has a different pattern_type."""
        conn = _open_inmemory_db()
        _seed_events_table(conn, [
            {
                "timestamp": _recent_ts(hours_ago=1),
                "type": "pattern_observation",
                "source": "chat-123",
                "metadata": {"pattern_type": "design_session"},
            }
        ])
        obs = _make_obs(pattern_type="brainstorm_mode", source="chat-123")
        assert _recent_duplicate_exists(conn, obs) is False
