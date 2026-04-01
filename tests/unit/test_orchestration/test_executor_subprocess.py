"""
Unit tests for the _dispatch_via_claude_p subprocess boundary.

These tests verify that all three subprocess exception paths (CalledProcessError,
TimeoutExpired, FileNotFoundError) result in the UoW being transitioned to 'failed'
with result.json written, and that the happy path transitions to 'ready-for-steward'.

Design: the tests inject a mock dispatcher callable into Executor(...) rather than
patching subprocess.run at the module level. This keeps the tests pure and independent
of how the module resolves the claude binary.

The tests do NOT import _dispatch_via_claude_p and exercise it directly — doing so
would require a real claude binary on PATH. Instead, they verify the contract:
"if the dispatcher callable raises X, the UoW must end up in state Y with result.json
present." The contract is stated in executor.py's docstring for _dispatch_via_claude_p
and enforced by _run_step_sequence's exception handler.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestration.registry import Registry, UoWStatus
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    _result_json_path,
    _noop_dispatcher,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers (reuse pattern from test_executor.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


def _make_artifact(uow_id: str, instructions: str = "Implement the thing") -> str:
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(db_path: Path, uow_id: str, workflow_artifact: str) -> None:
    """Insert a UoW in ready-for-executor state."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, estimated_runtime
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test UoW', 'done', ?, NULL)
            """,
            (uow_id, now, now, workflow_artifact),
        )
        conn.commit()
    finally:
        conn.close()


def _get_uow_status(db_path: Path, uow_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["status"] if row else ""
    finally:
        conn.close()


def _get_output_ref(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT output_ref FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["output_ref"] if row else None
    finally:
        conn.close()


def _read_result_json(output_ref: str) -> dict:
    result_path = _result_json_path(output_ref)
    return json.loads(result_path.read_text())


# ---------------------------------------------------------------------------
# CalledProcessError path
# ---------------------------------------------------------------------------

class TestCalledProcessErrorPath:
    """Subprocess exits non-zero → UoW must transition to 'failed', result.json written."""

    def test_uow_status_is_failed(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_cpe_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.CalledProcessError(returncode=1, cmd=["claude", "-p", "..."])

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        assert _get_uow_status(db_path, uow_id) == "failed"

    def test_result_json_written_with_failed_outcome(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_cpe_002"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.CalledProcessError(returncode=2, cmd=["claude", "-p", "..."])

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result = _read_result_json(output_ref)
        assert result["outcome"] == "failed"
        assert result["success"] is False

    def test_result_json_has_expected_fields(self, registry: Registry, db_path: Path) -> None:
        """result.json written by the exception handler uses outcome/success (result_writer schema)."""
        uow_id = "uow_cpe_003"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.CalledProcessError(returncode=1, cmd=["claude"])

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        # The exception handler writes via result_writer.write_result — uses outcome/success
        assert result["outcome"] == "failed"
        assert result["success"] is False


# ---------------------------------------------------------------------------
# TimeoutExpired path
# ---------------------------------------------------------------------------

class TestTimeoutExpiredPath:
    """Subprocess exceeds TTL → UoW must transition to 'failed', result.json written."""

    def test_uow_status_is_failed(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_toe_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def timeout_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.TimeoutExpired(cmd=["claude", "-p", "..."], timeout=7200)

        executor = Executor(registry, dispatcher=timeout_dispatcher)
        with pytest.raises(subprocess.TimeoutExpired):
            executor.execute_uow(uow_id)

        assert _get_uow_status(db_path, uow_id) == "failed"

    def test_result_json_written_with_failed_outcome(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_toe_002"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def timeout_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.TimeoutExpired(cmd=["claude", "-p", "..."], timeout=7200)

        executor = Executor(registry, dispatcher=timeout_dispatcher)
        with pytest.raises(subprocess.TimeoutExpired):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result = _read_result_json(output_ref)
        assert result["outcome"] == "failed"
        assert result["success"] is False

    def test_result_json_has_expected_fields(self, registry: Registry, db_path: Path) -> None:
        """result.json written by the exception handler uses outcome/success (result_writer schema)."""
        uow_id = "uow_toe_003"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def timeout_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=7200)

        executor = Executor(registry, dispatcher=timeout_dispatcher)
        with pytest.raises(subprocess.TimeoutExpired):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        # The exception handler writes via result_writer.write_result — uses outcome/success
        assert result["outcome"] == "failed"
        assert result["success"] is False


# ---------------------------------------------------------------------------
# FileNotFoundError path
# ---------------------------------------------------------------------------

class TestFileNotFoundErrorPath:
    """Claude binary not found → UoW must transition to 'failed', result.json written."""

    def test_uow_status_is_failed(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_fnf_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def missing_binary_dispatcher(instructions: str, uid: str) -> str:
            raise FileNotFoundError(2, "No such file or directory: 'claude'")

        executor = Executor(registry, dispatcher=missing_binary_dispatcher)
        with pytest.raises(FileNotFoundError):
            executor.execute_uow(uow_id)

        assert _get_uow_status(db_path, uow_id) == "failed"

    def test_result_json_written_with_failed_outcome(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_fnf_002"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def missing_binary_dispatcher(instructions: str, uid: str) -> str:
            raise FileNotFoundError(2, "No such file or directory: 'claude'")

        executor = Executor(registry, dispatcher=missing_binary_dispatcher)
        with pytest.raises(FileNotFoundError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result = _read_result_json(output_ref)
        assert result["outcome"] == "failed"
        assert result["success"] is False

    def test_result_json_has_expected_fields(self, registry: Registry, db_path: Path) -> None:
        """result.json written by the exception handler uses outcome/success (result_writer schema)."""
        uow_id = "uow_fnf_003"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def missing_binary_dispatcher(instructions: str, uid: str) -> str:
            raise FileNotFoundError(2, "No such file or directory: 'claude'")

        executor = Executor(registry, dispatcher=missing_binary_dispatcher)
        with pytest.raises(FileNotFoundError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        # The exception handler writes via result_writer.write_result — uses outcome/success
        assert result["outcome"] == "failed"
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    """Subprocess exits 0 → UoW transitions to 'ready-for-steward', result complete."""

    def test_uow_status_is_ready_for_steward(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_ok_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        dispatched: list[tuple[str, str]] = []

        def success_dispatcher(instructions: str, uid: str) -> str:
            dispatched.append((instructions, uid))
            return f"run-{uid}"

        executor = Executor(registry, dispatcher=success_dispatcher)
        result = executor.execute_uow(uow_id)

        assert _get_uow_status(db_path, uow_id) == "ready-for-steward"

    def test_result_json_written_with_complete_outcome(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_ok_002"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def success_dispatcher(instructions: str, uid: str) -> str:
            return f"run-{uid}"

        executor = Executor(registry, dispatcher=success_dispatcher)
        result = executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        data = _read_result_json(output_ref)
        assert data["outcome"] == "complete"
        assert data["success"] is True

    def test_instructions_are_passed_to_dispatcher(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_ok_003"
        expected_instructions = "Build the feature per spec."
        _insert_uow(db_path, uow_id, _make_artifact(uow_id, instructions=expected_instructions))

        received: list[str] = []

        def recording_dispatcher(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "run-ok"

        executor = Executor(registry, dispatcher=recording_dispatcher)
        executor.execute_uow(uow_id)

        assert len(received) == 1
        assert received[0] == expected_instructions

    def test_executor_id_recorded_in_result(self, registry: Registry, db_path: Path) -> None:
        uow_id = "uow_ok_004"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def success_dispatcher(instructions: str, uid: str) -> str:
            return "task-xyz-run-id"

        executor = Executor(registry, dispatcher=success_dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.executor_id == "task-xyz-run-id"

        output_ref = _get_output_ref(db_path, uow_id)
        data = _read_result_json(output_ref)
        assert data.get("executor_id") == "task-xyz-run-id"
