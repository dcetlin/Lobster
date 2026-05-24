"""
Unit tests for the activity_timeline view (issue #1108, migration 0023).

Behavior under test:

Migration:
- Migration 0023 creates the activity_timeline view in the WOS registry DB
- The view is queryable via standard SELECT after Registry() is initialized

View correctness:
- Returns rows from audit_log (event_type='uow_status_change')
- Returns rows from control_events (event_type as stored)
- audit_log rows carry outcome_category, token_usage, trigger_message_id
  joined from uow_registry
- control_events rows carry NULL for outcome_category, token_usage, trigger_message_id
- Rows are ordered by ts DESC (most recent first) across both sources
- trigger_message_id flows through from uow_registry into audit rows

Named constants:
- VIEW_NAME = 'activity_timeline' — the view name as created by migration 0023
- AUDIT_EVENT_TYPE = 'uow_status_change' — the fixed event_type for audit_log rows
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Named constants (spec §activity_timeline)
# ---------------------------------------------------------------------------

VIEW_NAME = "activity_timeline"
AUDIT_EVENT_TYPE = "uow_status_change"
EXAMPLE_TRIGGER_MESSAGE_ID = "1778365681821_8563"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path):
    """Create a fresh Registry backed by a temp DB with all migrations applied."""
    from orchestration.registry import Registry

    db_path = str(tmp_path / "test_registry.db")
    os.environ["REGISTRY_DB_PATH"] = db_path
    return Registry(db_path=db_path)


def _upsert_uow(
    registry,
    issue_number: int,
    *,
    trigger_message_id: str | None = None,
) -> str:
    """Upsert a UoW and return its uow_id."""
    from orchestration.registry import UpsertInserted

    result = registry.upsert(
        issue_number=issue_number,
        title=f"Test issue #{issue_number}",
        success_criteria="Tests pass with zero failures",
        register="operational",
        trigger_message_id=trigger_message_id,
    )
    assert isinstance(result, UpsertInserted)
    return result.id


def _query_timeline(registry) -> list[dict]:
    """Return all rows from activity_timeline ordered by ts DESC."""
    conn = registry._connect()
    try:
        rows = conn.execute(
            f"SELECT ts, event_type, entity_id, detail, outcome_category, "
            f"token_usage, trigger_message_id FROM {VIEW_NAME} ORDER BY ts DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _write_control_event(registry, event_type: str, payload: dict | None = None) -> None:
    """Write a row directly to control_events (simulates dispatcher action)."""
    conn = registry._connect()
    try:
        payload_json = json.dumps(payload) if payload is not None else None
        conn.execute(
            "INSERT INTO control_events (event_type, payload) VALUES (?, ?)",
            (event_type, payload_json),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration: activity_timeline view existence
# ---------------------------------------------------------------------------

class TestActivityTimelineMigration:
    """Migration 0023 creates the activity_timeline view."""

    def test_view_exists_after_registry_init(self, tmp_path):
        """Registry() auto-applies migrations; activity_timeline view must be created."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
                (VIEW_NAME,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, f"View '{VIEW_NAME}' was not created by migration"

    def test_view_is_queryable_with_empty_tables(self, tmp_path):
        """activity_timeline can be SELECTed even when both source tables are empty."""
        registry = _make_registry(tmp_path)
        rows = _query_timeline(registry)
        assert rows == []

    def test_view_columns_present(self, tmp_path):
        """View exposes the expected columns: ts, event_type, entity_id, detail,
        outcome_category, token_usage, trigger_message_id."""
        registry = _make_registry(tmp_path)
        # Create one audit row via a UoW upsert so the view has something to return
        _upsert_uow(registry, issue_number=1)
        rows = _query_timeline(registry)
        assert len(rows) >= 1
        expected_cols = {
            "ts", "event_type", "entity_id", "detail",
            "outcome_category", "token_usage", "trigger_message_id",
        }
        assert expected_cols.issubset(rows[0].keys())


# ---------------------------------------------------------------------------
# View correctness: audit_log rows
# ---------------------------------------------------------------------------

class TestActivityTimelineAuditRows:
    """activity_timeline surfaces audit_log rows as 'uow_status_change' events."""

    def test_upsert_produces_audit_row_in_timeline(self, tmp_path):
        """A UoW upsert writes an audit entry; the view returns it."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_uow(registry, issue_number=100)
        rows = _query_timeline(registry)
        audit_rows = [r for r in rows if r["event_type"] == AUDIT_EVENT_TYPE]
        assert len(audit_rows) >= 1
        entity_ids = [r["entity_id"] for r in audit_rows]
        assert uow_id in entity_ids

    def test_audit_row_event_type_is_uow_status_change(self, tmp_path):
        """All rows sourced from audit_log appear with event_type='uow_status_change'."""
        registry = _make_registry(tmp_path)
        _upsert_uow(registry, issue_number=101)
        rows = _query_timeline(registry)
        # Every row must have a non-None event_type
        assert all(r["event_type"] is not None for r in rows)
        # At least one row has the audit event_type
        assert any(r["event_type"] == AUDIT_EVENT_TYPE for r in rows)

    def test_audit_row_entity_id_matches_uow_id(self, tmp_path):
        """entity_id in audit rows matches the uow_id created by upsert."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_uow(registry, issue_number=102)
        rows = _query_timeline(registry)
        audit_rows = [r for r in rows if r["event_type"] == AUDIT_EVENT_TYPE]
        matched = [r for r in audit_rows if r["entity_id"] == uow_id]
        assert len(matched) >= 1


# ---------------------------------------------------------------------------
# View correctness: control_events rows
# ---------------------------------------------------------------------------

class TestActivityTimelineControlEventRows:
    """activity_timeline surfaces control_events rows with their own event_type."""

    def test_control_event_appears_in_timeline(self, tmp_path):
        """A row inserted into control_events is returned by the view."""
        registry = _make_registry(tmp_path)
        _write_control_event(registry, "wos_start")
        rows = _query_timeline(registry)
        control_rows = [r for r in rows if r["event_type"] == "wos_start"]
        assert len(control_rows) == 1

    def test_control_event_entity_id_is_string(self, tmp_path):
        """control_events.id (INTEGER PK) is cast to TEXT for entity_id."""
        registry = _make_registry(tmp_path)
        _write_control_event(registry, "wos_stop")
        rows = _query_timeline(registry)
        control_rows = [r for r in rows if r["event_type"] == "wos_stop"]
        assert len(control_rows) == 1
        # entity_id must be a non-empty string representation of the integer PK
        entity_id = control_rows[0]["entity_id"]
        assert isinstance(entity_id, str)
        assert entity_id.isdigit()

    def test_control_event_nulls_for_uow_fields(self, tmp_path):
        """Control rows have NULL outcome_category, token_usage, trigger_message_id."""
        registry = _make_registry(tmp_path)
        _write_control_event(registry, "wos_abort", {"uow_id": "uow_abc"})
        rows = _query_timeline(registry)
        control_rows = [r for r in rows if r["event_type"] == "wos_abort"]
        assert len(control_rows) == 1
        row = control_rows[0]
        assert row["outcome_category"] is None
        assert row["token_usage"] is None
        assert row["trigger_message_id"] is None

    def test_control_event_detail_carries_payload(self, tmp_path):
        """Control rows carry the payload JSON in the detail column."""
        registry = _make_registry(tmp_path)
        payload = {"uow_id": "uow_20260101_abc", "result": "closed"}
        _write_control_event(registry, "wos_abort", payload)
        rows = _query_timeline(registry)
        control_rows = [r for r in rows if r["event_type"] == "wos_abort"]
        assert len(control_rows) == 1
        # detail is the raw JSON payload stored in control_events.payload
        stored_payload = json.loads(control_rows[0]["detail"])
        assert stored_payload == payload


# ---------------------------------------------------------------------------
# View correctness: mixed rows and ordering
# ---------------------------------------------------------------------------

class TestActivityTimelineMixedRows:
    """activity_timeline returns rows from both sources, ordered by ts DESC."""

    def test_both_sources_appear_when_both_populated(self, tmp_path):
        """Rows from audit_log and control_events both appear in the view."""
        registry = _make_registry(tmp_path)
        _upsert_uow(registry, issue_number=200)
        _write_control_event(registry, "wos_start")
        rows = _query_timeline(registry)
        event_types = {r["event_type"] for r in rows}
        assert AUDIT_EVENT_TYPE in event_types
        assert "wos_start" in event_types

    def test_rows_ordered_ts_desc(self, tmp_path):
        """Rows are ordered by ts descending (most recent first)."""
        registry = _make_registry(tmp_path)
        _upsert_uow(registry, issue_number=201)
        _write_control_event(registry, "wos_start")
        rows = _query_timeline(registry)
        timestamps = [r["ts"] for r in rows]
        assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# trigger_message_id flows through from uow_registry
# ---------------------------------------------------------------------------

class TestTriggerMessageIdInTimeline:
    """trigger_message_id from uow_registry flows into activity_timeline audit rows."""

    def test_trigger_message_id_present_in_audit_rows(self, tmp_path):
        """When a UoW is created with trigger_message_id, it appears in timeline audit rows."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_uow(
            registry,
            issue_number=300,
            trigger_message_id=EXAMPLE_TRIGGER_MESSAGE_ID,
        )
        rows = _query_timeline(registry)
        audit_rows_for_uow = [
            r for r in rows
            if r["event_type"] == AUDIT_EVENT_TYPE and r["entity_id"] == uow_id
        ]
        assert len(audit_rows_for_uow) >= 1
        # All audit rows for this UoW must carry the trigger_message_id
        for row in audit_rows_for_uow:
            assert row["trigger_message_id"] == EXAMPLE_TRIGGER_MESSAGE_ID

    def test_trigger_message_id_null_in_audit_rows_when_not_set(self, tmp_path):
        """When a UoW is created without trigger_message_id, NULL appears in timeline."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_uow(registry, issue_number=301)
        rows = _query_timeline(registry)
        audit_rows_for_uow = [
            r for r in rows
            if r["event_type"] == AUDIT_EVENT_TYPE and r["entity_id"] == uow_id
        ]
        assert len(audit_rows_for_uow) >= 1
        for row in audit_rows_for_uow:
            assert row["trigger_message_id"] is None

    def test_two_uows_have_independent_trigger_message_ids_in_timeline(self, tmp_path):
        """Two UoWs with different trigger_message_ids appear independently in timeline."""
        registry = _make_registry(tmp_path)
        msg_id_a = "1778365681821_9001"
        msg_id_b = "1778365681821_9002"
        uow_id_a = _upsert_uow(registry, issue_number=310, trigger_message_id=msg_id_a)
        uow_id_b = _upsert_uow(registry, issue_number=311, trigger_message_id=msg_id_b)

        rows = _query_timeline(registry)
        rows_a = [
            r for r in rows
            if r["event_type"] == AUDIT_EVENT_TYPE and r["entity_id"] == uow_id_a
        ]
        rows_b = [
            r for r in rows
            if r["event_type"] == AUDIT_EVENT_TYPE and r["entity_id"] == uow_id_b
        ]
        assert all(r["trigger_message_id"] == msg_id_a for r in rows_a)
        assert all(r["trigger_message_id"] == msg_id_b for r in rows_b)
