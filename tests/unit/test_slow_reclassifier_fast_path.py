"""
Tests for the signal_type_hint fast path in slow_reclassifier.run_pass (PR #1032).

Covers:
- Events with signal_type_hint bypass pattern detection and receive confidence=high
- Fast-path events are excluded from pattern detection (events_for_pattern_detection filter)
- The written ClassificationTag uses the hint value as signal_type
- Events without signal_type_hint go through normal pattern detection (not fast-pathed)

These tests work against the pure functions and the DB-writing layer using an
in-memory SQLite database — no fastembed or sqlite-vec required.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from src.classifiers.slow_reclassifier import (
    ClassificationTag,
    EventRow,
    build_passthrough_tag,
    ensure_classification_table,
    write_tag,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_inmemory_db() -> sqlite3.Connection:
    """Open an in-memory SQLite connection with row_factory set."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _recent_ts(minutes_ago: float = 0) -> str:
    """Return a UTC ISO timestamp within the 6-hour look-back window."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _seed_events_table(conn: sqlite3.Connection, events: list[dict]) -> None:
    """Create a minimal events table and insert rows for testing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            subject TEXT,
            signal_type_hint TEXT
        )
    """)
    # Minimal events_vec table so write_pattern_event doesn't fail.
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS events_vec USING vec0(embedding float[384])")
    except Exception:
        conn.execute("CREATE TABLE IF NOT EXISTS events_vec (rowid INTEGER, embedding BLOB)")
    for ev in events:
        conn.execute(
            "INSERT INTO events (timestamp, type, source, content, metadata, subject, signal_type_hint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.get("timestamp", _recent_ts()),
                ev.get("type", "user_message"),
                ev.get("source", "telegram"),
                ev.get("content", "test content"),
                json.dumps(ev.get("metadata", {})),
                ev.get("subject"),
                ev.get("signal_type_hint"),
            ),
        )
    conn.commit()


def _seed_quick_tag(conn: sqlite3.Connection, event_id: int, signal_type: str = "system_observation") -> None:
    """Insert a quick-v1 classification tag so read_recent_events picks up the event."""
    conn.execute("""
        INSERT INTO classification_tags
            (entry_id, entry_type, classifier, significant, signal_a, signal_b,
             signal_c, signal_d, signal_e, confidence, notes, classified_at,
             signal_type, urgency, posture_hint)
        VALUES (?, 'event', 'quick-v1', 0, 0, 0, 0, 0, 0, 'low', '', ?, ?, 'normal', 'minimal_cognitive_friction')
    """, (str(event_id), datetime.now(timezone.utc).isoformat(), signal_type))
    conn.commit()


# ---------------------------------------------------------------------------
# Fast-path classification: signal_type and confidence
# ---------------------------------------------------------------------------

class TestFastPathClassification:
    """Events with signal_type_hint are written with confidence=high and the hint as signal_type."""

    def _run_pass_with_inmemory_db(self, events: list[dict]) -> sqlite3.Connection:
        """
        Seed an in-memory DB, run run_pass against it, and return the connection
        so the caller can inspect the written classification_tags.
        """
        conn = _open_inmemory_db()
        _seed_events_table(conn, events)
        ensure_classification_table(conn)

        # Seed a quick-v1 tag for each event so read_recent_events includes them.
        cursor = conn.execute("SELECT id FROM events")
        for row in cursor.fetchall():
            _seed_quick_tag(conn, row["id"])

        # run_pass needs: open_db (we skip that — pass conn directly), and
        # read_recent_events / write_tag.  Patch write_pattern_event so we
        # don't need sqlite-vec loaded.
        with patch("src.classifiers.slow_reclassifier.write_pattern_event", return_value=99):
            from src.classifiers.slow_reclassifier import run_pass
            run_pass(conn)

        return conn

    def test_hinted_event_receives_confidence_high(self):
        """An event with signal_type_hint=design_question gets a slow-v1 tag with confidence=high."""
        conn = self._run_pass_with_inmemory_db([
            {
                "timestamp": _recent_ts(minutes_ago=5),
                "type": "user_message",
                "source": "telegram",
                "content": "Should we use a relational schema here?",
                "signal_type_hint": "design_question",
            }
        ])
        row = conn.execute(
            "SELECT confidence, signal_type FROM classification_tags WHERE classifier = 'slow-v1'"
        ).fetchone()
        assert row is not None, "Expected a slow-v1 tag to be written for the hinted event"
        assert row["confidence"] == "high"
        assert row["signal_type"] == "design_question"

    def test_hinted_event_signal_type_matches_hint(self):
        """The signal_type in the written tag equals the hint value, not the quick-tag value."""
        conn = self._run_pass_with_inmemory_db([
            {
                "timestamp": _recent_ts(minutes_ago=5),
                "type": "voice_note",
                "source": "telegram",
                "content": "Quick brainstorm",
                "signal_type_hint": "voice_note",
            }
        ])
        row = conn.execute(
            "SELECT signal_type FROM classification_tags WHERE classifier = 'slow-v1'"
        ).fetchone()
        assert row["signal_type"] == "voice_note"

    def test_unhinted_event_does_not_receive_confidence_high(self):
        """An event without signal_type_hint goes through normal classification (confidence != high)."""
        conn = self._run_pass_with_inmemory_db([
            {
                "timestamp": _recent_ts(minutes_ago=5),
                "type": "user_message",
                "source": "telegram",
                "content": "Normal message without hint",
                "signal_type_hint": None,
            }
        ])
        row = conn.execute(
            "SELECT confidence FROM classification_tags WHERE classifier = 'slow-v1'"
        ).fetchone()
        # Unhinted events receive 'low' (passthrough) or 'medium' (pattern-revised), never 'high'.
        assert row is not None
        assert row["confidence"] != "high"

    def test_hinted_event_excluded_from_pattern_detection(self):
        """
        An event with signal_type_hint is tagged fast-path and not considered for
        cross-event pattern detection.

        Setup: 3 events tagged design_question (enough to trigger design_session).
        Two have signal_type_hint set; one does not. The hinted pair is excluded from
        pattern detection, leaving only 1 unhinted event — not enough for a design_session.
        The unhinted event should receive a passthrough (confidence=low) slow-v1 tag, NOT
        a pattern-revised (confidence=medium) tag.
        """
        # Use timestamps within the 6-hour look-back window (recent_ts subtracts from now).
        events = [
            {
                "timestamp": _recent_ts(minutes_ago=10 - i),  # e.g., 10, 9, 8 minutes ago
                "type": "user_message",
                "source": "telegram",
                "content": "Design question content",
                "signal_type_hint": "design_question" if i < 2 else None,
            }
            for i in range(3)
        ]
        conn = _open_inmemory_db()
        _seed_events_table(conn, events)
        ensure_classification_table(conn)

        cursor = conn.execute("SELECT id FROM events ORDER BY id")
        for row in cursor.fetchall():
            _seed_quick_tag(conn, row["id"], signal_type="design_question")

        with patch("src.classifiers.slow_reclassifier.write_pattern_event", return_value=99):
            from src.classifiers.slow_reclassifier import run_pass
            run_pass(conn)

        rows = conn.execute(
            "SELECT entry_id, confidence FROM classification_tags WHERE classifier = 'slow-v1' ORDER BY entry_id"
        ).fetchall()
        confidences = {int(r["entry_id"]): r["confidence"] for r in rows}

        # The 3rd event (no hint) should not get confidence=medium — there's only 1
        # unhinted event, not enough for a design_session pattern.
        unhinted_id = sorted(confidences.keys())[2]
        assert confidences[unhinted_id] == "low", (
            f"Unhinted event {unhinted_id} should have confidence=low (passthrough), "
            f"got {confidences[unhinted_id]!r}"
        )

        # The first two (hinted) events should have confidence=high.
        hinted_ids = sorted(confidences.keys())[:2]
        for eid in hinted_ids:
            assert confidences[eid] == "high", (
                f"Hinted event {eid} should have confidence=high, got {confidences[eid]!r}"
            )
