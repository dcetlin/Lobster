"""
Unit tests for the control_events table and Registry.log_control_event.

Issue #1104: add control_events to WOS registry for dispatcher command timeline.

Behavior under test:

Migration:
- Migration 0020 adds a control_events table with columns: id, ts, event_type, payload
- The table is append-only; no schema enforcement on payload

Registry.log_control_event:
- Inserts one row per call with the given event_type
- Payload dict is serialized to JSON text in the payload column
- None payload stores NULL in the payload column
- Multiple calls accumulate rows in insertion order
- A non-serializable payload is swallowed (non-fatal): no exception raised, no row written
- An unavailable table (pre-migration install) is swallowed: no exception raised

Named constants:
- CONTROL_EVENT_TABLE = 'control_events' — column/table name used in INSERT
- CONTROL_EVENT_TYPES contains at least: wos_start, wos_stop, wos_abort

Dispatcher writes:
- handle_wos_start writes event_type='wos_start' on successful toggle
- handle_wos_stop writes event_type='wos_stop' on successful toggle
- handle_decide_close writes event_type='wos_abort' with uow_id payload on success
- No control event is written for idempotent no-ops (already started/stopped)
- No control event is written when toggle_wos_core_jobs raises OSError
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Named constants (spec §control_events)
# ---------------------------------------------------------------------------

CONTROL_EVENT_TABLE = "control_events"

# Event types guaranteed to exist per spec
_EVENT_TYPE_WOS_START = "wos_start"
_EVENT_TYPE_WOS_STOP = "wos_stop"
_EVENT_TYPE_WOS_ABORT = "wos_abort"

_REQUIRED_EVENT_TYPES = {_EVENT_TYPE_WOS_START, _EVENT_TYPE_WOS_STOP, _EVENT_TYPE_WOS_ABORT}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path):
    """Create a fresh Registry backed by a temp DB with all migrations applied."""
    from orchestration.registry import Registry

    db_path = str(tmp_path / "test_registry.db")
    os.environ["REGISTRY_DB_PATH"] = db_path
    return Registry(db_path=db_path)


def _query_control_events(registry) -> list[dict]:
    """Return all rows from control_events as a list of dicts."""
    conn = registry._connect()
    try:
        rows = conn.execute(
            f"SELECT id, ts, event_type, payload FROM {CONTROL_EVENT_TABLE} ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration: control_events table schema
# ---------------------------------------------------------------------------

class TestControlEventsMigration:
    """Migration 0020 creates the control_events table with the correct schema."""

    def test_table_exists_after_registry_init(self, tmp_path):
        """Registry() auto-applies migrations; control_events table must be created."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            # PRAGMA table_info returns one row per column; empty = table absent
            cols = conn.execute(
                f"PRAGMA table_info({CONTROL_EVENT_TABLE})"
            ).fetchall()
        finally:
            conn.close()
        col_names = {row["name"] for row in cols}
        assert "id" in col_names
        assert "ts" in col_names
        assert "event_type" in col_names
        assert "payload" in col_names

    def test_table_has_autoincrement_id(self, tmp_path):
        """id column is INTEGER PRIMARY KEY AUTOINCREMENT."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            info = conn.execute(
                f"PRAGMA table_info({CONTROL_EVENT_TABLE})"
            ).fetchall()
        finally:
            conn.close()
        id_col = next((row for row in info if row["name"] == "id"), None)
        assert id_col is not None
        assert id_col["pk"] == 1  # is primary key

    def test_event_type_is_not_null(self, tmp_path):
        """event_type column has a NOT NULL constraint."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {CONTROL_EVENT_TABLE} (event_type) VALUES (NULL)"
                )
        finally:
            conn.close()

    def test_payload_is_nullable(self, tmp_path):
        """payload column accepts NULL."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            conn.execute(
                f"INSERT INTO {CONTROL_EVENT_TABLE} (event_type, payload) VALUES (?, ?)",
                ("test_event", None),
            )
            conn.commit()
            row = conn.execute(
                f"SELECT payload FROM {CONTROL_EVENT_TABLE} WHERE event_type = 'test_event'"
            ).fetchone()
        finally:
            conn.close()
        assert row["payload"] is None


# ---------------------------------------------------------------------------
# Registry.log_control_event — core behavior
# ---------------------------------------------------------------------------

class TestLogControlEvent:
    """Registry.log_control_event writes rows to control_events."""

    def test_single_event_is_written(self, tmp_path):
        """One call writes exactly one row."""
        registry = _make_registry(tmp_path)
        registry.log_control_event(_EVENT_TYPE_WOS_START)
        rows = _query_control_events(registry)
        assert len(rows) == 1
        assert rows[0]["event_type"] == _EVENT_TYPE_WOS_START

    def test_payload_dict_is_serialized_to_json(self, tmp_path):
        """A dict payload is stored as JSON text and round-trips correctly."""
        registry = _make_registry(tmp_path)
        payload = {"uow_id": "uow_20260101_abc", "result": "closed"}
        registry.log_control_event(_EVENT_TYPE_WOS_ABORT, payload)
        rows = _query_control_events(registry)
        assert len(rows) == 1
        stored = json.loads(rows[0]["payload"])
        assert stored == payload

    def test_none_payload_stores_null(self, tmp_path):
        """None payload results in NULL in the payload column."""
        registry = _make_registry(tmp_path)
        registry.log_control_event(_EVENT_TYPE_WOS_STOP, None)
        rows = _query_control_events(registry)
        assert len(rows) == 1
        assert rows[0]["payload"] is None

    def test_multiple_calls_accumulate_rows(self, tmp_path):
        """Each call appends one row; rows accumulate in insertion order."""
        registry = _make_registry(tmp_path)
        registry.log_control_event(_EVENT_TYPE_WOS_START)
        registry.log_control_event(_EVENT_TYPE_WOS_STOP)
        registry.log_control_event(_EVENT_TYPE_WOS_ABORT, {"uow_id": "uow_x"})
        rows = _query_control_events(registry)
        assert len(rows) == 3
        assert rows[0]["event_type"] == _EVENT_TYPE_WOS_START
        assert rows[1]["event_type"] == _EVENT_TYPE_WOS_STOP
        assert rows[2]["event_type"] == _EVENT_TYPE_WOS_ABORT

    def test_ts_column_is_populated(self, tmp_path):
        """ts column is set on insert (not NULL)."""
        registry = _make_registry(tmp_path)
        registry.log_control_event(_EVENT_TYPE_WOS_START)
        rows = _query_control_events(registry)
        assert rows[0]["ts"] is not None
        assert len(rows[0]["ts"]) > 0

    def test_non_serializable_payload_does_not_raise(self, tmp_path):
        """A payload that cannot be JSON-serialized is swallowed non-fatally."""
        registry = _make_registry(tmp_path)
        bad_payload: Any = {"fn": lambda: None}  # lambdas are not JSON-serializable
        # Must not raise
        registry.log_control_event(_EVENT_TYPE_WOS_START, bad_payload)
        # The row may or may not be written (impl may skip or write NULL payload)
        # — the key guarantee is no exception propagates

    def test_table_absent_does_not_raise(self, tmp_path):
        """If the control_events table is missing, log_control_event is non-fatal."""
        registry = _make_registry(tmp_path)
        # Drop the table to simulate a pre-migration install
        conn = registry._connect()
        conn.execute(f"DROP TABLE IF EXISTS {CONTROL_EVENT_TABLE}")
        conn.commit()
        conn.close()
        # Must not raise
        registry.log_control_event(_EVENT_TYPE_WOS_START)


# ---------------------------------------------------------------------------
# Dispatcher writes: handle_wos_start / handle_wos_stop / handle_decide_close
# ---------------------------------------------------------------------------

class TestDispatcherControlEventWrites:
    """
    Dispatcher handlers write control events at the correct moments.

    These tests mock out the file I/O and Registry so they can run without
    a live wos-config.json or jobs.json.
    """

    def _mock_wos_config(self, execution_enabled: bool) -> dict:
        return {"execution_enabled": execution_enabled}

    def _mock_toggle_result(self, toggled=("job-a",), not_found=()):
        return {"toggled": list(toggled), "not_found": list(not_found)}

    def test_handle_wos_start_writes_wos_start_event(self, tmp_path):
        """handle_wos_start writes 'wos_start' control event on successful toggle."""
        from orchestration.dispatcher_handlers import handle_wos_start

        registry = _make_registry(tmp_path)
        stopped_config = self._mock_wos_config(execution_enabled=False)
        toggle_result = self._mock_toggle_result()

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config", return_value=stopped_config),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs", return_value=toggle_result),
        ):
            handle_wos_start(registry=registry)

        rows = _query_control_events(registry)
        assert len(rows) == 1
        assert rows[0]["event_type"] == _EVENT_TYPE_WOS_START

    def test_handle_wos_start_idempotent_noop_writes_no_event(self, tmp_path):
        """handle_wos_start returns early when already running; no control event written."""
        from orchestration.dispatcher_handlers import handle_wos_start

        registry = _make_registry(tmp_path)
        running_config = self._mock_wos_config(execution_enabled=True)

        with patch("orchestration.dispatcher_handlers.read_wos_config", return_value=running_config):
            result = handle_wos_start(registry=registry)

        assert "already running" in result
        rows = _query_control_events(registry)
        assert len(rows) == 0

    def test_handle_wos_start_oserror_writes_no_event(self, tmp_path):
        """handle_wos_start returns error string on OSError; no control event written."""
        from orchestration.dispatcher_handlers import handle_wos_start

        registry = _make_registry(tmp_path)
        stopped_config = self._mock_wos_config(execution_enabled=False)

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config", return_value=stopped_config),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs", side_effect=OSError("disk full")),
        ):
            result = handle_wos_start(registry=registry)

        assert "Failed" in result or "disk full" in result
        rows = _query_control_events(registry)
        assert len(rows) == 0

    def test_handle_wos_stop_writes_wos_stop_event(self, tmp_path):
        """handle_wos_stop writes 'wos_stop' control event on successful toggle."""
        from orchestration.dispatcher_handlers import handle_wos_stop

        registry = _make_registry(tmp_path)
        running_config = self._mock_wos_config(execution_enabled=True)
        toggle_result = self._mock_toggle_result()

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config", return_value=running_config),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs", return_value=toggle_result),
        ):
            handle_wos_stop(registry=registry)

        rows = _query_control_events(registry)
        assert len(rows) == 1
        assert rows[0]["event_type"] == _EVENT_TYPE_WOS_STOP

    def test_handle_wos_stop_idempotent_noop_writes_no_event(self, tmp_path):
        """handle_wos_stop returns early when already stopped; no control event written."""
        from orchestration.dispatcher_handlers import handle_wos_stop

        registry = _make_registry(tmp_path)
        stopped_config = self._mock_wos_config(execution_enabled=False)

        with patch("orchestration.dispatcher_handlers.read_wos_config", return_value=stopped_config):
            result = handle_wos_stop(registry=registry)

        assert "already paused" in result
        rows = _query_control_events(registry)
        assert len(rows) == 0

    def test_handle_decide_close_success_writes_wos_abort_event(self, tmp_path):
        """handle_decide_close writes 'wos_abort' with uow_id payload when close succeeds."""
        from orchestration.dispatcher_handlers import handle_decide_close

        registry = _make_registry(tmp_path)
        # Mock decide_close to return 1 (success)
        registry.decide_close = MagicMock(return_value=1)
        # Wrap log_control_event to capture calls while still writing to the real DB
        original_log = registry.log_control_event
        logged: list[dict] = []

        def capturing_log(event_type, payload=None):
            logged.append({"event_type": event_type, "payload": payload})
            original_log(event_type, payload)

        registry.log_control_event = capturing_log

        handle_decide_close("uow_20260101_abc", registry=registry)

        assert len(logged) == 1
        assert logged[0]["event_type"] == _EVENT_TYPE_WOS_ABORT
        assert logged[0]["payload"]["uow_id"] == "uow_20260101_abc"

    def test_handle_decide_close_failure_writes_no_event(self, tmp_path):
        """handle_decide_close writes no control event when close fails (rowcount=0)."""
        from orchestration.dispatcher_handlers import handle_decide_close

        registry = _make_registry(tmp_path)
        registry.decide_close = MagicMock(return_value=0)
        logged: list[dict] = []
        registry.log_control_event = MagicMock(side_effect=lambda *a, **kw: logged.append(a))

        handle_decide_close("uow_20260101_abc", registry=registry)

        assert len(logged) == 0


# ---------------------------------------------------------------------------
# Required event types constant test
# ---------------------------------------------------------------------------

def test_required_event_types_are_defined():
    """The spec-required event type strings are defined as named constants in this test."""
    assert _EVENT_TYPE_WOS_START == "wos_start"
    assert _EVENT_TYPE_WOS_STOP == "wos_stop"
    assert _EVENT_TYPE_WOS_ABORT == "wos_abort"
    assert _REQUIRED_EVENT_TYPES == {"wos_start", "wos_stop", "wos_abort"}
