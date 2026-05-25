"""
Unit tests for handle_agent_failed handler and registry.record_agent_failed_kill.

WOS-UoW: uow_20260523_0d3a6e

Behavior verified:

handle_agent_failed:
- test_wos_uow_task_id_transitions_to_ready_for_steward:
  A message with task_id='wos-uow_...' calls record_agent_failed_kill and
  transitions the UoW to ready-for-steward.
- test_non_wos_task_id_no_registry_call:
  A message with task_id='ghost-mark-failed-abc' does not call record_agent_failed_kill.
- test_missing_task_id_no_registry_call:
  A message with no task_id field does not call record_agent_failed_kill.
- test_returns_mark_processed_for_wos_task_id:
  Returns action="mark_processed" when task_id has wos-uow_ prefix.
- test_returns_mark_processed_for_non_wos_task_id:
  Returns action="mark_processed" for non-WOS task_id.
- test_registry_error_still_returns_mark_processed:
  If record_agent_failed_kill raises, handler catches it and returns mark_processed.

registry.record_agent_failed_kill:
- test_transitions_executing_to_ready_for_steward:
  Executing UoW transitions to ready-for-steward.
- test_writes_agent_failed_audit_entry:
  Audit entry has event='agent_failed', from_status='executing',
  to_status='ready-for-steward', agent='reconciler'.
- test_audit_note_contains_uow_id_and_task_id:
  Audit note JSON contains uow_id and agent_task_id fields.
- test_returns_zero_on_race:
  Returns 0 when UoW is not in executing status (already transitioned).
- test_no_audit_entry_on_race:
  No audit entry written when rowcount=0.

route_wos_message:
- test_routes_agent_failed_before_spawn_gate:
  route_wos_message with type='agent_failed' returns action='mark_processed'
  without raising the spawn-gate error.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.registry import Registry
from src.orchestration.dispatcher_handlers import (
    handle_agent_failed,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_uow(
    db_path: Path,
    *,
    status: str = "executing",
) -> str:
    """Insert a UoW directly via SQLite. Returns the uow_id."""
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    issue_number = int(uuid.uuid4().int % 90000) + 10000

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria, started_at,
                 heartbeat_at, heartbeat_ttl, route_evidence, trigger, register, uow_mode)
            VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                    ?, ?, 'Test UoW', 'Test done.', ?,
                    ?, 300, '{}', '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


def _get_uow_status(db_path: Path, uow_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _get_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ?", (uow_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


# ---------------------------------------------------------------------------
# handle_agent_failed tests
# ---------------------------------------------------------------------------

class TestHandleAgentFailed:

    def test_wos_uow_task_id_transitions_to_ready_for_steward(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")
        task_id = f"wos-{uow_id}"
        msg = {"type": "agent_failed", "task_id": task_id, "agent_id": "agent-abc"}

        handle_agent_failed(msg, registry=registry)

        assert _get_uow_status(db_path, uow_id) == "ready-for-steward"

    def test_non_wos_task_id_no_registry_call(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")
        msg = {"type": "agent_failed", "task_id": "ghost-mark-failed-abc", "agent_id": "agent-abc"}
        mock_reg = MagicMock(spec=Registry)

        handle_agent_failed(msg, registry=mock_reg)

        mock_reg.record_agent_failed_kill.assert_not_called()
        # UoW status should be unchanged
        assert _get_uow_status(db_path, uow_id) == "executing"

    def test_missing_task_id_no_registry_call(self, db_path):
        msg = {"type": "agent_failed", "agent_id": "agent-abc"}
        mock_reg = MagicMock(spec=Registry)

        handle_agent_failed(msg, registry=mock_reg)

        mock_reg.record_agent_failed_kill.assert_not_called()

    def test_returns_mark_processed_for_wos_task_id(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")
        task_id = f"wos-{uow_id}"
        msg = {"type": "agent_failed", "task_id": task_id}

        result = handle_agent_failed(msg, registry=registry)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "agent_failed"

    def test_returns_mark_processed_for_non_wos_task_id(self):
        msg = {"type": "agent_failed", "task_id": "ghost-mark-failed-xyz"}
        mock_reg = MagicMock(spec=Registry)

        result = handle_agent_failed(msg, registry=mock_reg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "agent_failed"

    def test_registry_error_still_returns_mark_processed(self):
        task_id = "wos-uow_test_deadbeef"
        msg = {"type": "agent_failed", "task_id": task_id}

        mock_reg = MagicMock(spec=Registry)
        mock_reg.record_agent_failed_kill.side_effect = RuntimeError("DB locked")

        result = handle_agent_failed(msg, registry=mock_reg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "agent_failed"


# ---------------------------------------------------------------------------
# registry.record_agent_failed_kill tests
# ---------------------------------------------------------------------------

class TestRecordAgentFailedKill:

    def test_transitions_executing_to_ready_for_steward(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")

        rows = registry.record_agent_failed_kill(uow_id, agent_task_id=f"wos-{uow_id}")

        assert rows == 1
        assert _get_uow_status(db_path, uow_id) == "ready-for-steward"

    def test_writes_agent_failed_audit_entry(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")

        registry.record_agent_failed_kill(uow_id, agent_task_id=f"wos-{uow_id}")

        entries = _get_audit_entries(db_path, uow_id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["event"] == "agent_failed"
        assert entry["from_status"] == "executing"
        assert entry["to_status"] == "ready-for-steward"
        assert entry["agent"] == "reconciler"

    def test_audit_note_contains_uow_id_and_task_id(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="executing")
        task_id = f"wos-{uow_id}"

        registry.record_agent_failed_kill(uow_id, agent_task_id=task_id)

        entries = _get_audit_entries(db_path, uow_id)
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert note["uow_id"] == uow_id
        assert note["agent_task_id"] == task_id

    def test_returns_zero_on_race(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="ready-for-steward")

        rows = registry.record_agent_failed_kill(uow_id, agent_task_id=f"wos-{uow_id}")

        assert rows == 0

    def test_no_audit_entry_on_race(self, registry, db_path):
        uow_id = _insert_uow(db_path, status="ready-for-steward")

        registry.record_agent_failed_kill(uow_id, agent_task_id=f"wos-{uow_id}")

        entries = _get_audit_entries(db_path, uow_id)
        assert len(entries) == 0


# ---------------------------------------------------------------------------
# route_wos_message tests
# ---------------------------------------------------------------------------

class TestRouteWosMessageAgentFailed:

    def test_agent_failed_registered_in_dispatch_table(self):
        assert "agent_failed" in WOS_MESSAGE_TYPE_DISPATCH

    def test_routes_agent_failed_before_spawn_gate(self, registry, db_path):
        """route_wos_message with type='agent_failed' must return mark_processed
        without raising a spawn-gate ValueError."""
        uow_id = _insert_uow(db_path, status="executing")
        task_id = f"wos-{uow_id}"
        msg = {
            "type": "agent_failed",
            "task_id": task_id,
            "agent_id": "agent-abc",
        }

        # Patch handle_agent_failed to avoid Registry() default construction
        with patch(
            "src.orchestration.dispatcher_handlers.handle_agent_failed",
            return_value={"action": "mark_processed", "message_type": "agent_failed"},
        ) as mock_handler:
            result = route_wos_message(msg)

        mock_handler.assert_called_once_with(msg)
        assert result["action"] == "mark_processed"
        assert result["message_type"] == "agent_failed"
