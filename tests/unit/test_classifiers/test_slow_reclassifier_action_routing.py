"""
Tests for threshold-gated action routing in slow_reclassifier.

Covers:
- compute_pattern_confidence returns correct confidence level
- route_pattern_to_action creates task for design_session with HIGH confidence
- route_pattern_to_action flags meta_thread for digest with HIGH confidence
- Dedup: second call on same pattern_event_id is a no-op
- MEDIUM confidence patterns produce no action
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.classifiers.slow_reclassifier import (
    ACTION_CONFIDENCE_MINIMUM,
    DESIGN_SESSION_THRESHOLD,
    LOBSTER_WORKSPACE,
    META_THREAD_THRESHOLD,
    PatternObservation,
    compute_pattern_confidence,
    ensure_pattern_actions_table,
    route_pattern_to_action,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_obs(
    pattern_type: str = "design_session",
    event_ids: list[int] | None = None,
    source: str = "test-source",
) -> PatternObservation:
    """Build a PatternObservation with sensible defaults."""
    if event_ids is None:
        event_ids = list(range(1, DESIGN_SESSION_THRESHOLD * 2 + 1))
    return PatternObservation(
        pattern_type=pattern_type,
        source=source,
        event_ids=event_ids,
        signal_type=pattern_type,
        urgency="normal",
        posture_hint="structural_coherence",
        detected_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc),
        valence="neutral",
    )


@pytest.fixture
def db():
    """In-memory SQLite with events table and pattern_actions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            type      TEXT,
            source    TEXT,
            content   TEXT,
            metadata  TEXT
        )
    """)
    ensure_pattern_actions_table(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputePatternConfidence:
    """compute_pattern_confidence returns HIGH when events >= threshold * 2."""

    def test_high_when_at_double_threshold(self):
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD * 2)),
        )
        assert compute_pattern_confidence(obs) == "HIGH"

    def test_medium_when_below_double_threshold(self):
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD)),
        )
        assert compute_pattern_confidence(obs) == "MEDIUM"

    def test_high_above_double_threshold(self):
        obs = _make_obs(
            pattern_type="meta_thread",
            event_ids=list(range(META_THREAD_THRESHOLD * 3)),
        )
        assert compute_pattern_confidence(obs) == "HIGH"


class TestRoutePatternCreatesTask:
    """route_pattern_to_action creates a task for design_session with HIGH confidence."""

    def test_creates_task_file(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.classifiers.slow_reclassifier.LOBSTER_WORKSPACE", tmp_path
        )
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD * 2)),
        )
        pattern_event_id = 100

        route_pattern_to_action(db, obs, pattern_event_id)

        tasks_path = tmp_path / "tasks.json"
        assert tasks_path.exists()
        tasks = json.loads(tasks_path.read_text())
        assert len(tasks) == 1
        assert tasks[0]["source"] == "slow-reclassifier"
        assert "Design Session" in tasks[0]["subject"]

    def test_records_action_in_db(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.classifiers.slow_reclassifier.LOBSTER_WORKSPACE", tmp_path
        )
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD * 2)),
        )
        pattern_event_id = 101

        route_pattern_to_action(db, obs, pattern_event_id)

        row = db.execute(
            "SELECT * FROM pattern_actions WHERE pattern_event_id = ?",
            (pattern_event_id,),
        ).fetchone()
        assert row is not None
        assert row["action_type"] == "task"


class TestRoutePatternFlagsDigest:
    """route_pattern_to_action writes a digest_flag event for meta_thread."""

    def test_writes_digest_flag_event(self, db):
        obs = _make_obs(
            pattern_type="meta_thread",
            event_ids=list(range(META_THREAD_THRESHOLD * 2)),
        )
        pattern_event_id = 200

        route_pattern_to_action(db, obs, pattern_event_id)

        row = db.execute(
            "SELECT * FROM events WHERE type = 'digest_flag'"
        ).fetchone()
        assert row is not None
        assert "meta_thread" in row["content"]

    def test_records_digest_flag_action(self, db):
        obs = _make_obs(
            pattern_type="meta_thread",
            event_ids=list(range(META_THREAD_THRESHOLD * 2)),
        )
        pattern_event_id = 201

        route_pattern_to_action(db, obs, pattern_event_id)

        row = db.execute(
            "SELECT * FROM pattern_actions WHERE pattern_event_id = ?",
            (pattern_event_id,),
        ).fetchone()
        assert row is not None
        assert row["action_type"] == "digest_flag"


class TestDeduplication:
    """Calling route_pattern_to_action twice on same pattern_event_id acts only once."""

    def test_second_call_is_noop(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.classifiers.slow_reclassifier.LOBSTER_WORKSPACE", tmp_path
        )
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD * 2)),
        )
        pattern_event_id = 300

        route_pattern_to_action(db, obs, pattern_event_id)
        route_pattern_to_action(db, obs, pattern_event_id)

        tasks = json.loads((tmp_path / "tasks.json").read_text())
        assert len(tasks) == 1  # only one task created

        count = db.execute(
            "SELECT COUNT(*) FROM pattern_actions WHERE pattern_event_id = ?",
            (pattern_event_id,),
        ).fetchone()[0]
        assert count == 1


class TestMediumConfidenceNoAction:
    """route_pattern_to_action is a no-op when confidence is MEDIUM."""

    def test_no_action_for_medium_confidence(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.classifiers.slow_reclassifier.LOBSTER_WORKSPACE", tmp_path
        )
        # Use fewer events than threshold * 2 to get MEDIUM
        obs = _make_obs(
            pattern_type="design_session",
            event_ids=list(range(DESIGN_SESSION_THRESHOLD)),
        )
        pattern_event_id = 400

        route_pattern_to_action(db, obs, pattern_event_id)

        # No task file created
        tasks_path = tmp_path / "tasks.json"
        assert not tasks_path.exists()

        # No action recorded
        row = db.execute(
            "SELECT * FROM pattern_actions WHERE pattern_event_id = ?",
            (pattern_event_id,),
        ).fetchone()
        assert row is None
