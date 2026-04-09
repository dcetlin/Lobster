"""
Unit tests for wos_completion.py — G2 gap from Sprint 4 test harness design.

Covers: maybe_complete_wos_uow — the deferred execution_complete transition for
the async inbox dispatch path.

Behavior under test:
- task_id not starting with "wos-" → no-op (non-WOS task)
- status != "success" → no-op (only successes advance the UoW)
- UoW not found in registry → no-op (logs and returns)
- DB not found → no-op (no WOS install or test env)
- UoW in "executing" + status="success" → transitions to "ready-for-steward"
- UoW not in "executing" status → skipped silently (duplicate write_result or
  TTL recovery already handled it)
- Registry error → logs warning, does not raise

Named constants mirror the names in wos_completion.py to anchor tests to the spec.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.wos_completion import (
    WOS_TASK_ID_PREFIX,
    WRITE_RESULT_SUCCESS_STATUS,
    maybe_complete_wos_uow,
)
from orchestration.registry import Registry, UoWStatus, UpsertInserted


# ---------------------------------------------------------------------------
# Constants — named after spec values so failures are self-documenting
# ---------------------------------------------------------------------------

_NON_WOS_TASK_ID = "some-other-task-123"
_WOS_TASK_ID_FOR_UNKNOWN = f"{WOS_TASK_ID_PREFIX}does-not-exist"
_FAILURE_STATUS = "error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_uow_at_status(registry: Registry, target_status: str, output_dir: Path) -> str:
    """
    Seed a UoW and advance it to the given status using set_status_direct.

    Uses direct status manipulation to keep the test helper independent of
    the Executor's internal claim logic. The output_ref is written via a direct
    SQL UPDATE when needed for the executing path.

    Returns the uow_id.
    """
    import sqlite3

    result = registry.upsert(
        issue_number=9901,
        title="Completion test UoW",
        success_criteria="maybe_complete_wos_uow transitions it",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    registry.approve(uow_id)

    if target_status == "executing":
        # To call transition_to_executing we need the UoW to be in 'active' first.
        # Set output_ref directly so complete_uow has a valid value to use.
        output_ref = str(output_dir / f"{uow_id}.json")
        registry.set_status_direct(uow_id, "active")
        # Write output_ref directly — bypasses Executor internal logic for test isolation
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute(
            "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
            (output_ref, uow_id),
        )
        conn.commit()
        conn.close()
        registry.transition_to_executing(uow_id, "mock-executor-001")
    elif target_status not in ("pending",):
        registry.set_status_direct(uow_id, target_status)

    return uow_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMaybeCompleteWosUow:
    """Behavioral tests for maybe_complete_wos_uow."""

    def test_non_wos_task_id_is_ignored(self, tmp_path: Path) -> None:
        """
        A task_id that does not start with WOS_TASK_ID_PREFIX must not touch
        the registry — this is the primary filtering gate.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # No exception raised, registry untouched
            maybe_complete_wos_uow(_NON_WOS_TASK_ID, WRITE_RESULT_SUCCESS_STATUS)

        # DB was created by Registry() init, but no UoW rows should exist
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM uow_registry").fetchone()[0]
        conn.close()
        assert count == 0, "Non-WOS task_id must not create any UoW records"

    def test_error_status_does_not_advance_executing_uow(self, tmp_path: Path) -> None:
        """
        A write_result with status="error" must leave the UoW in 'executing'.

        Only successful completions trigger the executing → ready-for-steward
        transition. Failed write_results leave the UoW for TTL recovery.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, _FAILURE_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.EXECUTING, (
            f"Error write_result must leave UoW in executing, got {uow.status}"
        )

    def test_executing_uow_with_success_transitions_to_ready_for_steward(
        self, tmp_path: Path
    ) -> None:
        """
        Core behavior: a UoW in 'executing' status advances to 'ready-for-steward'
        when write_result arrives with status='success'.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Executing UoW + success write_result must reach ready-for-steward, "
            f"got {uow.status}"
        )

    def test_execution_complete_audit_entry_is_written(self, tmp_path: Path) -> None:
        """
        After a successful completion, the audit_log must contain an
        'execution_complete' event for the UoW.
        """
        import sqlite3

        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        events = [
            row["event"]
            for row in conn.execute(
                "SELECT event FROM audit_log WHERE uow_id = ?", (uow_id,)
            ).fetchall()
        ]
        conn.close()

        assert "execution_complete" in events, (
            f"audit_log must contain 'execution_complete' after write_result success. "
            f"Found events: {events}"
        )

    def test_uow_not_found_is_silently_skipped(self, tmp_path: Path) -> None:
        """
        A WOS task_id that has no matching UoW in the registry must be skipped
        without raising an exception.
        """
        db_path = tmp_path / "registry.db"
        Registry(db_path)  # Initialize DB schema

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # Must not raise
            maybe_complete_wos_uow(_WOS_TASK_ID_FOR_UNKNOWN, WRITE_RESULT_SUCCESS_STATUS)

    def test_uow_already_ready_for_steward_is_skipped(self, tmp_path: Path) -> None:
        """
        If the UoW is already in 'ready-for-steward' (e.g. TTL recovery already
        advanced it), a duplicate write_result must not change the status.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "ready-for-steward", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # Must not raise, must not change status
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Duplicate write_result on non-executing UoW must not change status, "
            f"got {uow.status}"
        )

    def test_uow_in_done_status_is_skipped(self, tmp_path: Path) -> None:
        """
        A UoW already in 'done' status must be silently skipped (not double-transitioned).
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        # Manually advance to done to simulate prior completion
        registry.set_status_direct(uow_id, "done")

        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status.value == "done", (
            f"Done UoW must not be transitioned by duplicate write_result, "
            f"got {uow.status}"
        )

    def test_missing_db_does_not_raise(self, tmp_path: Path) -> None:
        """
        When the registry DB does not exist (no WOS install), maybe_complete_wos_uow
        must return silently without raising.
        """
        nonexistent_db = tmp_path / "no_such.db"
        task_id = f"{WOS_TASK_ID_PREFIX}uow_20260101_abc123"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(nonexistent_db)}):
            # Must not raise
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

    def test_duplicate_success_write_result_does_not_double_transition(
        self, tmp_path: Path
    ) -> None:
        """
        Calling maybe_complete_wos_uow twice with the same task_id and status=success
        must be idempotent: the second call is silently skipped because the UoW is
        already in 'ready-for-steward'.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)
            # Second call — must not raise, must not change status further
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Idempotency violated: status after duplicate call should be "
            f"ready-for-steward, got {uow.status}"
        )

    def test_registry_exception_does_not_propagate(self, tmp_path: Path) -> None:
        """
        If the registry raises an unexpected exception, maybe_complete_wos_uow must
        log a warning and return — write_result delivery must not be blocked by
        registry update failures.
        """
        db_path = tmp_path / "registry.db"

        task_id = f"{WOS_TASK_ID_PREFIX}uow_20260101_abc123"

        # wos_completion.py imports Registry lazily inside the function via
        # "from orchestration.registry import Registry", so we patch at the
        # source module level to intercept the instantiation.
        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}), \
             patch("orchestration.registry.Registry", side_effect=RuntimeError("test error")):
            # Must not raise
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

    def test_wos_task_id_prefix_constant_is_correct(self) -> None:
        """
        The WOS_TASK_ID_PREFIX constant must match the naming convention used by
        route_wos_message in dispatcher_handlers.py ("wos-").
        """
        assert WOS_TASK_ID_PREFIX == "wos-", (
            f"WOS_TASK_ID_PREFIX must be 'wos-', got {WOS_TASK_ID_PREFIX!r}"
        )

    def test_write_result_success_status_constant_is_correct(self) -> None:
        """
        The WRITE_RESULT_SUCCESS_STATUS constant must match the status string
        sent by a completing subagent ("success").
        """
        assert WRITE_RESULT_SUCCESS_STATUS == "success", (
            f"WRITE_RESULT_SUCCESS_STATUS must be 'success', got {WRITE_RESULT_SUCCESS_STATUS!r}"
        )
