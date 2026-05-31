"""
Tests for WOS Stage 2 Event-Native Nervous System (issue #1351).

Coverage:
  event_log schema
  - test_event_log_migration_creates_table: migration SQL creates table with required columns
  - test_event_log_dedup_index: unique (event_type, dedup_key) constraint prevents duplicates
  - test_event_log_allows_null_dedup_key: NULL dedup_key rows are not deduped

  wos_events.py — emit functions
  - test_emit_issue_created_writes_inbox_message: emit_issue_created writes inbox file
  - test_emit_issue_created_records_event_log: emit_issue_created inserts event_log row
  - test_emit_issue_created_deduplication: second call with same issue_number is no-op
  - test_emit_issue_created_returns_none_on_duplicate: returns None for duplicate
  - test_emit_uow_completed_writes_inbox_message: emit_uow_completed writes inbox file
  - test_emit_uow_completed_records_event_log: inserts event_log row
  - test_emit_uow_completed_deduplication: second call with same uow_id is no-op
  - test_emit_capacity_available_writes_inbox_message: emit_capacity_available writes inbox file
  - test_emit_capacity_available_deduplication: second call with same freed_uow_id is no-op
  - test_mark_event_consumed_sets_timestamps: mark_event_consumed updates consumed_at

  dispatcher_handlers.py — handler functions
  - test_handle_wos_issue_created_returns_mark_processed: handler returns action=mark_processed
  - test_handle_wos_uow_completed_returns_mark_processed: handler returns action=mark_processed
  - test_handle_wos_capacity_available_returns_mark_processed: handler returns action=mark_processed
  - test_handle_wos_issue_created_calls_mark_event_consumed: handler calls mark_event_consumed
  - test_handle_wos_uow_completed_calls_mark_event_consumed: handler calls mark_event_consumed

  route_wos_message routing
  - test_route_wos_message_routes_wos_issue_created: routes to handle_wos_issue_created
  - test_route_wos_message_routes_wos_uow_completed: routes to handle_wos_uow_completed
  - test_route_wos_message_routes_wos_capacity_available: routes to handle_wos_capacity_available
  - test_route_wos_message_handles_handler_exception: exception in handler returns mark_processed

  wos-event-poller.py
  - test_poller_exits_when_disabled: is_job_enabled=False causes immediate return
  - test_poller_run_cycle_emits_issue_events: new issues trigger emit_issue_created
  - test_poller_run_cycle_emits_uow_events: terminal UoWs trigger emit_uow_completed
  - test_poller_run_cycle_emits_capacity_events: UoW completion with free slots triggers capacity
  - test_poller_advances_cursor_on_success: cursor is written after successful cycle
  - test_poller_does_not_advance_cursor_on_failure: cursor not written on exception
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_log_schema() -> str:
    """Return the event_log migration SQL."""
    migration_path = _REPO_ROOT / "src" / "orchestration" / "migrations" / "0030_event_log.sql"
    return migration_path.read_text()


def _create_event_log_db(path: Path) -> sqlite3.Connection:
    """Create a minimal SQLite DB with the event_log table applied."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_make_event_log_schema())
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# event_log schema tests
# ---------------------------------------------------------------------------

class TestEventLogSchema:
    def test_event_log_migration_creates_table(self, tmp_path):
        """Migration SQL creates the event_log table with all required columns."""
        db_path = tmp_path / "registry.db"
        conn = _create_event_log_db(db_path)
        # Check table exists by querying it
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'").fetchall()
        assert len(rows) == 1, "event_log table should exist after migration"
        # Check column names
        cols = {row[1] for row in conn.execute("PRAGMA table_info(event_log)").fetchall()}
        required_cols = {"event_id", "event_type", "payload", "emitted_at", "consumed_at", "consumer_task_id", "dedup_key"}
        assert required_cols == cols, f"Missing columns: {required_cols - cols}"
        conn.close()

    def test_event_log_dedup_index_prevents_duplicates(self, tmp_path):
        """UNIQUE(event_type, dedup_key) prevents inserting duplicate events."""
        db_path = tmp_path / "registry.db"
        conn = _create_event_log_db(db_path)

        conn.execute(
            "INSERT INTO event_log (event_id, event_type, payload, emitted_at, dedup_key) VALUES (?, ?, '{}', datetime('now'), ?)",
            ("id-1", "wos_issue_created", "42"),
        )
        conn.commit()

        # INSERT OR IGNORE should silently skip the duplicate
        conn.execute(
            "INSERT OR IGNORE INTO event_log (event_id, event_type, payload, emitted_at, dedup_key) VALUES (?, ?, '{}', datetime('now'), ?)",
            ("id-2", "wos_issue_created", "42"),
        )
        conn.commit()

        rows = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='wos_issue_created' AND dedup_key='42'").fetchone()
        assert rows[0] == 1, "Duplicate (event_type, dedup_key) should be ignored"
        conn.close()

    def test_event_log_allows_null_dedup_key(self, tmp_path):
        """NULL dedup_key rows are not subject to the unique constraint."""
        db_path = tmp_path / "registry.db"
        conn = _create_event_log_db(db_path)

        for i in range(3):
            conn.execute(
                "INSERT INTO event_log (event_id, event_type, payload, emitted_at, dedup_key) VALUES (?, ?, '{}', datetime('now'), NULL)",
                (str(uuid.uuid4()), "wos_issue_created"),
            )
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
        assert count == 3, "NULL dedup_key rows should all be inserted"
        conn.close()


# ---------------------------------------------------------------------------
# wos_events.py tests
# ---------------------------------------------------------------------------

class TestEmitIssueCreated:
    def test_emit_issue_created_writes_inbox_message(self, tmp_path):
        """emit_issue_created writes a JSON file to the inbox directory."""
        from src.orchestration.wos_events import emit_issue_created

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_issue_created(
                issue_number=100,
                issue_url="https://github.com/dcetlin/Lobster/issues/100",
                title="test issue",
                labels=["wos:uow"],
                db_path=db_path,
            )

        assert event_id is not None, "event_id should be returned on first emission"
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, "Exactly one inbox file should be written"

        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "wos_issue_created"
        assert msg["issue_number"] == 100
        assert msg["labels"] == ["wos:uow"]

    def test_emit_issue_created_records_event_log(self, tmp_path):
        """emit_issue_created inserts a row into event_log."""
        from src.orchestration.wos_events import emit_issue_created

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_issue_created(
                issue_number=200,
                issue_url="https://github.com/dcetlin/Lobster/issues/200",
                title="another issue",
                labels=["wos:uow"],
                db_path=db_path,
            )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM event_log WHERE event_id=?", (event_id,)).fetchone()
        conn.close()

        assert row is not None, "event_log row should be written"
        assert row[1] == "wos_issue_created"  # event_type
        assert row[6] == "200"                # dedup_key = str(issue_number)

    def test_emit_issue_created_deduplication(self, tmp_path):
        """Second call with the same issue_number skips inbox write (dedup)."""
        from src.orchestration.wos_events import emit_issue_created

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            first_id = emit_issue_created(
                issue_number=300,
                issue_url="https://github.com/dcetlin/Lobster/issues/300",
                title="dupe issue",
                labels=["wos:uow"],
                db_path=db_path,
            )
            second_id = emit_issue_created(
                issue_number=300,  # same issue_number
                issue_url="https://github.com/dcetlin/Lobster/issues/300",
                title="dupe issue",
                labels=["wos:uow"],
                db_path=db_path,
            )

        assert first_id is not None
        assert second_id is None, "Duplicate emission should return None"
        # Only one inbox file should have been written
        assert len(list(inbox_dir.glob("*.json"))) == 1

    def test_emit_issue_created_returns_none_on_duplicate(self, tmp_path):
        """emit_issue_created returns None rather than raising on duplicate dedup_key."""
        from src.orchestration.wos_events import emit_issue_created

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            emit_issue_created(
                issue_number=400,
                issue_url="https://github.com/dcetlin/Lobster/issues/400",
                title="dup test",
                labels=[],
                db_path=db_path,
            )
            result = emit_issue_created(
                issue_number=400,
                issue_url="https://github.com/dcetlin/Lobster/issues/400",
                title="dup test",
                labels=[],
                db_path=db_path,
            )

        assert result is None


class TestEmitUowCompleted:
    def test_emit_uow_completed_writes_inbox_message(self, tmp_path):
        """emit_uow_completed writes a JSON inbox message with correct fields."""
        from src.orchestration.wos_events import emit_uow_completed

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        uow_id = "uow_20260531_abc123"
        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_uow_completed(
                uow_id=uow_id,
                outcome="done",
                register="code",
                output_ref="/some/output.json",
                db_path=db_path,
            )

        assert event_id is not None
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "wos_uow_completed"
        assert msg["uow_id"] == uow_id
        assert msg["outcome"] == "done"
        assert msg["register"] == "code"

    def test_emit_uow_completed_records_event_log(self, tmp_path):
        """emit_uow_completed inserts a row with dedup_key=uow_id."""
        from src.orchestration.wos_events import emit_uow_completed

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        uow_id = "uow_test_1"
        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_uow_completed(
                uow_id=uow_id,
                outcome="failed",
                register="research",
                db_path=db_path,
            )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT dedup_key FROM event_log WHERE event_id=?", (event_id,)).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == uow_id  # dedup_key should be uow_id

    def test_emit_uow_completed_deduplication(self, tmp_path):
        """Second emit_uow_completed for same uow_id is silently dropped."""
        from src.orchestration.wos_events import emit_uow_completed

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        uow_id = "uow_dedup_test"
        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            first = emit_uow_completed(uow_id=uow_id, outcome="done", register="code", db_path=db_path)
            second = emit_uow_completed(uow_id=uow_id, outcome="done", register="code", db_path=db_path)

        assert first is not None
        assert second is None
        assert len(list(inbox_dir.glob("*.json"))) == 1


class TestEmitCapacityAvailable:
    def test_emit_capacity_available_writes_inbox_message(self, tmp_path):
        """emit_capacity_available writes a JSON inbox message with correct fields."""
        from src.orchestration.wos_events import emit_capacity_available

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        freed_uow_id = "uow_freed_1"
        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_capacity_available(
                freed_uow_id=freed_uow_id,
                current_active_count=1,
                max_parallel=2,
                db_path=db_path,
            )

        assert event_id is not None
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "wos_capacity_available"
        assert msg["freed_uow_id"] == freed_uow_id
        assert msg["current_active_count"] == 1
        assert msg["max_parallel"] == 2

    def test_emit_capacity_available_deduplication(self, tmp_path):
        """Second emit_capacity_available for same freed_uow_id is silently dropped."""
        from src.orchestration.wos_events import emit_capacity_available

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        freed_uow_id = "uow_freed_dedup"
        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            first = emit_capacity_available(freed_uow_id=freed_uow_id, current_active_count=0, max_parallel=2, db_path=db_path)
            second = emit_capacity_available(freed_uow_id=freed_uow_id, current_active_count=0, max_parallel=2, db_path=db_path)

        assert first is not None
        assert second is None


class TestMarkEventConsumed:
    def test_mark_event_consumed_sets_consumed_at(self, tmp_path):
        """mark_event_consumed sets consumed_at on an unconsumed event."""
        from src.orchestration.wos_events import emit_issue_created, mark_event_consumed

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):
            event_id = emit_issue_created(
                issue_number=999,
                issue_url="https://github.com/dcetlin/Lobster/issues/999",
                title="consume test",
                labels=[],
                db_path=db_path,
            )

        assert event_id is not None

        # Verify consumed_at is NULL before calling mark_event_consumed
        conn = sqlite3.connect(str(db_path))
        row_before = conn.execute(
            "SELECT consumed_at, consumer_task_id FROM event_log WHERE event_id=?",
            (event_id,),
        ).fetchone()
        conn.close()
        assert row_before[0] is None, "consumed_at should be NULL before mark_event_consumed"

        mark_event_consumed(event_id, "test-consumer", db_path=db_path)

        conn = sqlite3.connect(str(db_path))
        row_after = conn.execute(
            "SELECT consumed_at, consumer_task_id FROM event_log WHERE event_id=?",
            (event_id,),
        ).fetchone()
        conn.close()
        assert row_after[0] is not None, "consumed_at should be set after mark_event_consumed"
        assert row_after[1] == "test-consumer"


# ---------------------------------------------------------------------------
# dispatcher_handlers.py handler tests
# ---------------------------------------------------------------------------

class TestDispatcherEventHandlers:
    """Tests for the three new Stage 2 event handlers in dispatcher_handlers.py."""

    def _load_handlers(self):
        """Import dispatcher_handlers with minimal env patching."""
        # Ensure the module can be imported without a live DB
        import importlib
        import src.orchestration.dispatcher_handlers as dh
        # Re-import to get fresh state
        importlib.reload(dh)
        return dh

    def test_handle_wos_issue_created_returns_mark_processed(self):
        """handle_wos_issue_created always returns action=mark_processed."""
        from src.orchestration.dispatcher_handlers import handle_wos_issue_created

        msg = {
            "type": "wos_issue_created",
            "event_id": str(uuid.uuid4()),
            "issue_number": 42,
            "issue_url": "https://github.com/dcetlin/Lobster/issues/42",
            "title": "test issue",
            "labels": ["wos:uow"],
            "triggered_at": "2026-05-31T00:00:00+00:00",
        }

        with patch("src.orchestration.dispatcher_handlers.handle_wos_issue_created.__module__"):
            pass  # just import

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = handle_wos_issue_created(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_issue_created"

    def test_handle_wos_uow_completed_returns_mark_processed(self):
        """handle_wos_uow_completed always returns action=mark_processed."""
        from src.orchestration.dispatcher_handlers import handle_wos_uow_completed

        msg = {
            "type": "wos_uow_completed",
            "event_id": str(uuid.uuid4()),
            "uow_id": "uow_20260531_abc",
            "outcome": "done",
            "register": "code",
            "triggered_at": "2026-05-31T00:00:00+00:00",
        }

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = handle_wos_uow_completed(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_uow_completed"

    def test_handle_wos_capacity_available_returns_mark_processed(self):
        """handle_wos_capacity_available always returns action=mark_processed."""
        from src.orchestration.dispatcher_handlers import handle_wos_capacity_available

        msg = {
            "type": "wos_capacity_available",
            "event_id": str(uuid.uuid4()),
            "freed_uow_id": "uow_freed_123",
            "freed_at": "2026-05-31T00:00:00+00:00",
            "current_active_count": 1,
            "max_parallel": 2,
        }

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = handle_wos_capacity_available(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_capacity_available"

    def test_handle_wos_issue_created_calls_mark_event_consumed(self):
        """handle_wos_issue_created calls mark_event_consumed with the event_id."""
        from src.orchestration.dispatcher_handlers import handle_wos_issue_created

        event_id = str(uuid.uuid4())
        msg = {
            "type": "wos_issue_created",
            "event_id": event_id,
            "issue_number": 55,
            "title": "check consumed call",
        }

        with patch("src.orchestration.wos_events.mark_event_consumed") as mock_consume:
            handle_wos_issue_created(msg)

        mock_consume.assert_called_once_with(event_id, consumer_task_id="dispatcher-wos_issue_created")

    def test_handle_wos_uow_completed_calls_mark_event_consumed(self):
        """handle_wos_uow_completed calls mark_event_consumed with the event_id."""
        from src.orchestration.dispatcher_handlers import handle_wos_uow_completed

        event_id = str(uuid.uuid4())
        msg = {
            "type": "wos_uow_completed",
            "event_id": event_id,
            "uow_id": "uow_test_consume",
            "outcome": "done",
        }

        with patch("src.orchestration.wos_events.mark_event_consumed") as mock_consume:
            handle_wos_uow_completed(msg)

        mock_consume.assert_called_once_with(event_id, consumer_task_id="dispatcher-wos_uow_completed")


# ---------------------------------------------------------------------------
# route_wos_message routing tests
# ---------------------------------------------------------------------------

class TestRouteWosMessageStage2:
    """Tests for route_wos_message routing of Stage 2 event types."""

    def test_route_wos_message_routes_wos_issue_created(self):
        """route_wos_message dispatches wos_issue_created to handle_wos_issue_created."""
        from src.orchestration.dispatcher_handlers import route_wos_message

        msg = {
            "type": "wos_issue_created",
            "event_id": str(uuid.uuid4()),
            "issue_number": 100,
            "issue_url": "https://github.com/dcetlin/Lobster/issues/100",
            "title": "routed issue",
            "labels": ["wos:uow"],
        }

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = route_wos_message(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_issue_created"

    def test_route_wos_message_routes_wos_uow_completed(self):
        """route_wos_message dispatches wos_uow_completed to handle_wos_uow_completed."""
        from src.orchestration.dispatcher_handlers import route_wos_message

        msg = {
            "type": "wos_uow_completed",
            "event_id": str(uuid.uuid4()),
            "uow_id": "uow_route_test",
            "outcome": "failed",
            "register": "research",
        }

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = route_wos_message(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_uow_completed"

    def test_route_wos_message_routes_wos_capacity_available(self):
        """route_wos_message dispatches wos_capacity_available to handle_wos_capacity_available."""
        from src.orchestration.dispatcher_handlers import route_wos_message

        msg = {
            "type": "wos_capacity_available",
            "event_id": str(uuid.uuid4()),
            "freed_uow_id": "uow_cap_route",
            "current_active_count": 0,
            "max_parallel": 2,
        }

        with patch("src.orchestration.wos_events.mark_event_consumed"):
            result = route_wos_message(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_capacity_available"

    def test_route_wos_message_handler_exception_returns_mark_processed(self):
        """route_wos_message catches handler exceptions for Stage 2 types and returns mark_processed."""
        from src.orchestration.dispatcher_handlers import route_wos_message

        msg = {
            "type": "wos_issue_created",
            "event_id": str(uuid.uuid4()),
            "issue_number": 0,  # valid msg; we mock the handler to raise
        }

        with patch("src.orchestration.dispatcher_handlers.handle_wos_issue_created", side_effect=RuntimeError("boom")):
            result = route_wos_message(msg)

        assert result["action"] == "mark_processed"
        assert result["message_type"] == "wos_issue_created"


# ---------------------------------------------------------------------------
# wos-event-poller.py tests
# ---------------------------------------------------------------------------

def _load_poller_module():
    """Import wos-event-poller with patched heavy dependencies."""
    import importlib
    import importlib.util

    mocks_paths = MagicMock()
    mocks_paths.REGISTRY_DB = Path("/nonexistent/registry.db")
    mocks_paths.WOS_CONFIG = Path("/nonexistent/wos-config.json")

    mocks = {
        "src.utils.jobs": MagicMock(),
        "src.orchestration.paths": mocks_paths,
    }

    mod_name = "wos_event_poller"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        mod_name,
        _REPO_ROOT / "scheduled-tasks" / "wos-event-poller.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so dataclass type resolution works
    sys.modules[mod_name] = mod

    with patch.dict("sys.modules", mocks):
        spec.loader.exec_module(mod)

    return mod


class TestWosEventPoller:
    """Tests for wos-event-poller.py poll logic."""

    def test_poller_exits_when_disabled(self, tmp_path, capsys):
        """main() returns immediately when is_job_enabled returns False."""
        mod = _load_poller_module()
        mod.is_job_enabled = MagicMock(return_value=False)
        mod._read_cursor = MagicMock()
        mod.run_poll_cycle = MagicMock()

        mod.main.__globals__["is_job_enabled"] = MagicMock(return_value=False)
        # Patch sys.argv to avoid argparse picking up pytest args
        with patch("sys.argv", ["wos-event-poller.py"]):
            mod.main()

        mod.run_poll_cycle.assert_not_called()

    def test_poller_run_cycle_emits_issue_events(self, tmp_path):
        """run_poll_cycle emits wos_issue_created for new GitHub issues found."""
        mod = _load_poller_module()

        from dataclasses import dataclass

        new_issue = mod.NewIssue(
            issue_number=1000,
            issue_url="https://github.com/dcetlin/Lobster/issues/1000",
            title="new wos issue",
            labels=["wos:uow"],
            created_at="2026-05-31T12:00:00+00:00",
        )

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.object(mod, "_fetch_new_wos_issues", return_value=[new_issue]), \
             patch.object(mod, "_fetch_newly_completed_uows", return_value=[]), \
             patch.object(mod, "_count_active_uows", return_value=0), \
             patch.object(mod, "_read_max_parallel", return_value=2), \
             patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):

            issue_count, uow_count, capacity_count = mod.run_poll_cycle(
                since="2026-05-31T11:00:00+00:00",
                db_path=db_path,
            )

        assert issue_count == 1
        assert uow_count == 0
        assert capacity_count == 0

    def test_poller_run_cycle_emits_uow_events(self, tmp_path):
        """run_poll_cycle emits wos_uow_completed for newly terminal UoWs."""
        mod = _load_poller_module()

        completed_uow = mod.CompletedUoW(
            uow_id="uow_20260531_poller1",
            outcome="done",
            register="code",
            output_ref=None,
            completed_at="2026-05-31T12:00:00+00:00",
        )

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.object(mod, "_fetch_new_wos_issues", return_value=[]), \
             patch.object(mod, "_fetch_newly_completed_uows", return_value=[completed_uow]), \
             patch.object(mod, "_count_active_uows", return_value=2), \
             patch.object(mod, "_read_max_parallel", return_value=2), \
             patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):

            issue_count, uow_count, capacity_count = mod.run_poll_cycle(
                since="2026-05-31T11:00:00+00:00",
                db_path=db_path,
            )

        assert issue_count == 0
        assert uow_count == 1
        # active_count == max_parallel so no capacity event
        assert capacity_count == 0

    def test_poller_run_cycle_emits_capacity_events(self, tmp_path):
        """run_poll_cycle emits wos_capacity_available when active < max_parallel."""
        mod = _load_poller_module()

        completed_uow = mod.CompletedUoW(
            uow_id="uow_20260531_cap1",
            outcome="done",
            register="code",
            output_ref=None,
            completed_at="2026-05-31T12:00:00+00:00",
        )

        db_path = tmp_path / "registry.db"
        _create_event_log_db(db_path)
        inbox_dir = tmp_path / "inbox"

        with patch.object(mod, "_fetch_new_wos_issues", return_value=[]), \
             patch.object(mod, "_fetch_newly_completed_uows", return_value=[completed_uow]), \
             patch.object(mod, "_count_active_uows", return_value=1), \
             patch.object(mod, "_read_max_parallel", return_value=2), \
             patch.dict("os.environ", {"LOBSTER_INBOX_DIR": str(inbox_dir)}):

            issue_count, uow_count, capacity_count = mod.run_poll_cycle(
                since="2026-05-31T11:00:00+00:00",
                db_path=db_path,
            )

        assert uow_count == 1
        # active_count (1) < max_parallel (2) → capacity event emitted
        assert capacity_count == 1

    def test_poller_advances_cursor_on_success(self, tmp_path):
        """main() writes a new since-cursor after a successful poll cycle."""
        mod = _load_poller_module()

        cursor_path = tmp_path / "wos-event-poller-cursor.json"
        mod._cursor_path = MagicMock(return_value=cursor_path)
        mod._read_cursor = MagicMock(return_value="2026-05-31T10:00:00+00:00")
        mock_write = MagicMock()
        mod._write_cursor = mock_write
        mod.run_poll_cycle = MagicMock(return_value=(0, 0, 0))
        mod.is_job_enabled = MagicMock(return_value=True)

        with patch("sys.argv", ["wos-event-poller.py"]):
            mod.main()

        mock_write.assert_called_once()
        written_cursor = mock_write.call_args[0][0]
        assert written_cursor, "Cursor should be advanced after successful run"

    def test_poller_does_not_advance_cursor_on_failure(self, tmp_path):
        """main() does not write a cursor when run_poll_cycle raises."""
        mod = _load_poller_module()

        mock_write = MagicMock()
        mod._write_cursor = mock_write
        mod._read_cursor = MagicMock(return_value="2026-05-31T10:00:00+00:00")
        mod.run_poll_cycle = MagicMock(side_effect=RuntimeError("db error"))
        mod.is_job_enabled = MagicMock(return_value=True)

        with patch("sys.argv", ["wos-event-poller.py"]):
            with pytest.raises(RuntimeError):
                mod.main()

        mock_write.assert_not_called()
