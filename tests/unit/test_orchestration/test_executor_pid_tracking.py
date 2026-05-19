"""
Unit tests for executor PID tracking and wos abort command (migration 0020).

Tests are derived from the spec — each test name states the behavior, not the
mechanism. Implementation details (SQL, signal numbers) are hidden behind the
Registry public API where possible.

Behaviors tested:

PID storage:
- test_migration_adds_executor_pid_column: migration 0020 adds executor_pid column
- test_get_executor_pid_returns_none_before_set: None returned before any write
- test_set_executor_pid_stores_pid: set stores a retrievable integer PID
- test_set_executor_pid_overwrites_previous: subsequent set replaces earlier PID
- test_clear_executor_pid_removes_pid: clear sets executor_pid to NULL
- test_clear_executor_pid_idempotent_when_null: clear is a no-op when already NULL
- test_get_executor_pid_returns_none_for_missing_uow: None for nonexistent UoW

Kill path:
- test_kill_executor_returns_false_when_no_pid: no PID stored → False
- test_kill_executor_sends_sigterm_to_process_group: kills via killpg and returns True
- test_kill_executor_clears_pid_after_successful_kill: PID cleared after kill
- test_kill_executor_handles_process_already_gone: ProcessLookupError → False, PID cleared
- test_kill_executor_clears_pid_when_process_already_gone: PID cleared even on ProcessLookupError
- test_kill_executor_returns_false_on_permission_error: PermissionError → False, PID retained
- test_kill_executor_preserves_pid_on_permission_error: PID NOT cleared on PermissionError (process still alive)

Dispatcher command parsing:
- test_parse_wos_abort_extracts_uow_id: parses "wos abort <uow_id>"
- test_parse_wos_abort_case_insensitive: matches regardless of case
- test_parse_wos_abort_returns_none_for_non_matching: None for unrelated commands
- test_parse_wos_abort_returns_none_for_incomplete: None for bare "wos abort"

Dispatcher command handler:
- test_handle_wos_abort_no_running_process: reply when no PID stored
- test_handle_wos_abort_kills_running_process: kill succeeds → success reply
- test_handle_wos_abort_process_already_gone: ProcessLookupError → already-gone reply
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import Registry
from src.orchestration.dispatcher_handlers import (
    parse_wos_abort_command,
    handle_wos_abort,
)


# ---------------------------------------------------------------------------
# Named constants from spec (migration 0020)
# ---------------------------------------------------------------------------

#: Column name added by migration 0020
EXECUTOR_PID_COLUMN = "executor_pid"

#: Fake PID used in tests — realistic but distinguishable from real PIDs
FAKE_PID = 99999


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Registry with all migrations applied (including 0020 executor_pid)."""
    return Registry(db_path)


def _insert_uow(db_path: Path, *, status: str = "active") -> str:
    """Insert a minimal UoW row directly via SQLite. Returns uow_id."""
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
                 created_at, updated_at, summary, success_criteria, route_evidence,
                 trigger, register, uow_mode)
            VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                    ?, ?, 'Test UoW', 'Test done.', '{}',
                    '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

def test_migration_adds_executor_pid_column(registry: Registry, db_path: Path) -> None:
    """Migration 0020 adds the executor_pid column to uow_registry."""
    conn = sqlite3.connect(str(db_path))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(uow_registry)").fetchall()]
    finally:
        conn.close()
    assert EXECUTOR_PID_COLUMN in cols, (
        f"executor_pid column not found in uow_registry after migration. "
        f"Columns present: {cols}"
    )


# ---------------------------------------------------------------------------
# PID storage tests
# ---------------------------------------------------------------------------

def test_get_executor_pid_returns_none_before_set(
    registry: Registry, db_path: Path
) -> None:
    """get_executor_pid returns None for a UoW that has never had a PID written."""
    uow_id = _insert_uow(db_path)
    assert registry.get_executor_pid(uow_id) is None


def test_set_executor_pid_stores_pid(registry: Registry, db_path: Path) -> None:
    """set_executor_pid writes a PID that get_executor_pid can read back."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)
    assert registry.get_executor_pid(uow_id) == FAKE_PID


def test_set_executor_pid_overwrites_previous(
    registry: Registry, db_path: Path
) -> None:
    """set_executor_pid replaces an earlier PID with the new value."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)
    registry.set_executor_pid(uow_id, FAKE_PID + 1)
    assert registry.get_executor_pid(uow_id) == FAKE_PID + 1


def test_clear_executor_pid_removes_pid(registry: Registry, db_path: Path) -> None:
    """clear_executor_pid sets executor_pid to NULL so get returns None."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)
    registry.clear_executor_pid(uow_id)
    assert registry.get_executor_pid(uow_id) is None


def test_clear_executor_pid_idempotent_when_null(
    registry: Registry, db_path: Path
) -> None:
    """clear_executor_pid is a no-op when executor_pid is already NULL."""
    uow_id = _insert_uow(db_path)
    # PID is NULL from the start — clear should not raise.
    registry.clear_executor_pid(uow_id)
    assert registry.get_executor_pid(uow_id) is None


def test_get_executor_pid_returns_none_for_missing_uow(registry: Registry) -> None:
    """get_executor_pid returns None for a UoW ID that does not exist."""
    assert registry.get_executor_pid("uow_nonexistent_000000") is None


# ---------------------------------------------------------------------------
# Kill path tests
# ---------------------------------------------------------------------------

def test_kill_executor_returns_false_when_no_pid(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor returns False when no PID is stored (executor_pid IS NULL)."""
    uow_id = _insert_uow(db_path)
    result = registry.kill_executor(uow_id)
    assert result is False


def test_kill_executor_sends_sigterm_to_process_group(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor calls os.killpg with SIGTERM when a PID is stored, returns True."""
    import signal
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID) as mock_getpgid, \
         patch("os.killpg") as mock_killpg:
        result = registry.kill_executor(uow_id)

    assert result is True
    mock_getpgid.assert_called_once_with(FAKE_PID)
    mock_killpg.assert_called_once_with(FAKE_PID, signal.SIGTERM)


def test_kill_executor_clears_pid_after_successful_kill(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor clears executor_pid after a successful SIGTERM send."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg"):
        registry.kill_executor(uow_id)

    assert registry.get_executor_pid(uow_id) is None


def test_kill_executor_handles_process_already_gone(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor returns False (not an error) when process has already exited."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=ProcessLookupError("no such process")):
        result = registry.kill_executor(uow_id)

    assert result is False


def test_kill_executor_clears_pid_when_process_already_gone(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor clears the stale PID even when ProcessLookupError is raised."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=ProcessLookupError("no such process")):
        registry.kill_executor(uow_id)

    assert registry.get_executor_pid(uow_id) is None


def test_kill_executor_returns_false_on_permission_error(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor returns False when os.killpg raises PermissionError (process still running but unowned)."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=PermissionError("operation not permitted")):
        result = registry.kill_executor(uow_id)

    assert result is False


def test_kill_executor_preserves_pid_on_permission_error(
    registry: Registry, db_path: Path
) -> None:
    """kill_executor does NOT clear executor_pid when PermissionError is raised.

    The process is still running — clearing the PID would make a future abort
    attempt silently return False with no kill attempt (no PID stored).
    """
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=PermissionError("operation not permitted")):
        registry.kill_executor(uow_id)

    # PID must still be present — process is alive but unowned
    assert registry.get_executor_pid(uow_id) == FAKE_PID


# ---------------------------------------------------------------------------
# Dispatcher command parsing tests
# ---------------------------------------------------------------------------

def test_parse_wos_abort_extracts_uow_id() -> None:
    """parse_wos_abort_command extracts the uow_id from a well-formed command."""
    uow_id = parse_wos_abort_command("wos abort uow_20260426_abc123")
    assert uow_id == "uow_20260426_abc123"


def test_parse_wos_abort_case_insensitive() -> None:
    """parse_wos_abort_command matches regardless of case."""
    uow_id = parse_wos_abort_command("WOS ABORT uow_20260426_abc123")
    assert uow_id == "uow_20260426_abc123"


def test_parse_wos_abort_returns_none_for_non_matching() -> None:
    """parse_wos_abort_command returns None for unrelated commands."""
    assert parse_wos_abort_command("wos start") is None
    assert parse_wos_abort_command("wos stop") is None
    assert parse_wos_abort_command("diagnose uow_20260426_abc123") is None
    assert parse_wos_abort_command("abort uow_20260426_abc123") is None


def test_parse_wos_abort_returns_none_for_incomplete() -> None:
    """parse_wos_abort_command returns None when no uow_id follows 'abort'."""
    assert parse_wos_abort_command("wos abort") is None
    assert parse_wos_abort_command("wos abort   ") is None


# ---------------------------------------------------------------------------
# Dispatcher command handler tests
# ---------------------------------------------------------------------------

def test_handle_wos_abort_no_running_process(
    registry: Registry, db_path: Path
) -> None:
    """handle_wos_abort reports 'no running process' when executor_pid is NULL."""
    uow_id = _insert_uow(db_path)
    reply = handle_wos_abort(uow_id, registry=registry)
    assert "no running process" in reply.lower()
    assert uow_id in reply


def test_handle_wos_abort_kills_running_process(
    registry: Registry, db_path: Path
) -> None:
    """handle_wos_abort reports success when kill_executor kills the process."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg"):
        reply = handle_wos_abort(uow_id, registry=registry)

    assert "aborted" in reply.lower() or "sigterm" in reply.lower()
    assert uow_id in reply
    assert str(FAKE_PID) in reply


def test_handle_wos_abort_process_already_gone(
    registry: Registry, db_path: Path
) -> None:
    """handle_wos_abort reports process-already-gone when kill_executor returns False due to ProcessLookupError."""
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=ProcessLookupError("no such process")):
        reply = handle_wos_abort(uow_id, registry=registry)

    # Reply should indicate the process was already gone (not an error)
    assert "already" in reply.lower() or "gone" in reply.lower() or "exited" in reply.lower()
    assert uow_id in reply


def test_handle_wos_abort_permission_error_on_kill(
    registry: Registry, db_path: Path
) -> None:
    """handle_wos_abort reports permission denied (not ProcessLookupError) when kill_executor returns False due to PermissionError.

    The PermissionError case means:
    - The process is STILL RUNNING (not gone)
    - executor_pid is RETAINED (not cleared)
    - The correct message must reflect all three facts

    This test guards against the pre-fix bug where both False-return cases
    produced the ProcessLookupError message ("was already gone", "pid cleared"),
    which was wrong on all three counts for the PermissionError path.
    """
    uow_id = _insert_uow(db_path)
    registry.set_executor_pid(uow_id, FAKE_PID)

    with patch("os.getpgid", return_value=FAKE_PID), \
         patch("os.killpg", side_effect=PermissionError("operation not permitted")):
        reply = handle_wos_abort(uow_id, registry=registry)

    # Must mention permission denial — not ProcessLookupError language
    assert "permission" in reply.lower() or "denied" in reply.lower(), (
        f"Expected 'permission' or 'denied' in reply, got: {reply!r}"
    )
    # Must NOT claim the process is gone (it isn't)
    assert "already gone" not in reply.lower() and "processlookup" not in reply.lower(), (
        f"Reply incorrectly used ProcessLookupError language for PermissionError: {reply!r}"
    )
    # Must mention the UoW ID and PID
    assert uow_id in reply
    assert str(FAKE_PID) in reply
    # Must NOT claim PID was cleared (it wasn't)
    assert "executor_pid has been cleared" not in reply, (
        f"Reply incorrectly claimed PID cleared on PermissionError: {reply!r}"
    )
