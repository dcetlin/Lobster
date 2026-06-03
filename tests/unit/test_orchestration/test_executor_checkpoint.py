"""
Tests for executor checkpoint integration — Issue #1323.

Coverage:
- test_claim_writes_checkpoint_ref: After _claim(), checkpoint_ref column
  in uow_registry is non-null and points to a checkpoint.json path.
- test_claim_creates_checkpoint_dir: After _claim(), the checkpoint directory
  exists on disk.
- test_claim_writes_initial_checkpoint_file: After _claim(), checkpoint.json
  exists at checkpoint_ref path with all steps in 'pending' status and
  completion_fraction == 0.0.
- test_checkpoint_steps_match_canonical_plan: Initial checkpoint has exactly
  the six canonical steps (read_issue → write_result).
- test_prompt_contains_checkpoint_path: The assembled subagent prompt contains
  a CHECKPOINT_PATH: line pointing to the correct path.
- test_preamble_contains_checkpoint_protocol: _FUNCTIONAL_ENGINEER_PREAMBLE
  includes the Checkpoint Protocol section with the atomic write instructions.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import orchestration.checkpoint as checkpoint_module
from orchestration.registry import Registry
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    _noop_dispatcher,
    _FUNCTIONAL_ENGINEER_PREAMBLE,
)

# ---------------------------------------------------------------------------
# Helpers — reuse the _insert_uow / _get_uow_field pattern from test_executor.py
# ---------------------------------------------------------------------------

def _make_artifact(uow_id: str, instructions: str = "Do the thing") -> str:
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": "functional-engineer",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(db_path: Path, uow_id: str) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?,
                      'Test UoW', 'test done', ?)
            """,
            (uow_id, now, now, _make_artifact(uow_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _get_uow_field(db_path: Path, uow_id: str, field: str) -> object:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {field} FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row[field] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


@pytest.fixture
def isolated_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect checkpoint writes to a temp directory.

    Monkeypatches checkpoint_module.CHECKPOINTS_DIR so that
    checkpoint_path(uow_id) and write_checkpoint() write to tmp_path/checkpoints/
    rather than ~/lobster-workspace/orchestration/checkpoints/.
    """
    checkpoints_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoint_module, "CHECKPOINTS_DIR", checkpoints_dir)
    return checkpoints_dir


# ---------------------------------------------------------------------------
# Test: checkpoint_ref is written to DB at claim time
# ---------------------------------------------------------------------------

class TestClaimWritesCheckpointRef:
    def test_checkpoint_ref_is_non_null_after_claim(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """_claim() must write a non-null checkpoint_ref to uow_registry."""
        uow_id = "uow_cp_ref_001"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        checkpoint_ref = _get_uow_field(db_path, uow_id, "checkpoint_ref")
        assert checkpoint_ref is not None
        assert checkpoint_ref != ""

    def test_checkpoint_ref_points_to_checkpoint_json(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """checkpoint_ref must end with 'checkpoint.json'."""
        uow_id = "uow_cp_ref_002"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        checkpoint_ref = _get_uow_field(db_path, uow_id, "checkpoint_ref")
        assert str(checkpoint_ref).endswith("checkpoint.json"), (
            f"checkpoint_ref should end with 'checkpoint.json', got: {checkpoint_ref!r}"
        )

    def test_checkpoint_ref_contains_uow_id(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """checkpoint_ref path must contain the uow_id as a path segment."""
        uow_id = "uow_cp_ref_003"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        checkpoint_ref = _get_uow_field(db_path, uow_id, "checkpoint_ref")
        assert uow_id in str(checkpoint_ref), (
            f"checkpoint_ref should contain uow_id={uow_id!r}, got: {checkpoint_ref!r}"
        )


# ---------------------------------------------------------------------------
# Test: checkpoint directory is created at claim time
# ---------------------------------------------------------------------------

class TestClaimCreatesCheckpointDir:
    def test_checkpoint_directory_exists_after_claim(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """After _claim(), the checkpoint directory must exist on disk."""
        uow_id = "uow_cp_dir_001"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        expected_dir = isolated_checkpoints / uow_id
        assert expected_dir.is_dir(), (
            f"Expected checkpoint dir {expected_dir} to exist after claim"
        )


# ---------------------------------------------------------------------------
# Test: initial checkpoint.json is written correctly
# ---------------------------------------------------------------------------

class TestClaimWritesInitialCheckpointFile:
    def test_checkpoint_json_exists_after_claim(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """checkpoint.json must exist at checkpoint_ref path after _claim()."""
        uow_id = "uow_cp_file_001"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        expected_file = isolated_checkpoints / uow_id / "checkpoint.json"
        assert expected_file.exists(), (
            f"checkpoint.json should exist at {expected_file} after claim"
        )

    def test_all_steps_are_pending_in_initial_checkpoint(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """All steps in the initial checkpoint must have status='pending'."""
        uow_id = "uow_cp_file_002"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())

        assert data["steps"], "Initial checkpoint must have a non-empty steps list"
        for step in data["steps"]:
            assert step["status"] == "pending", (
                f"Step {step['name']!r} should be 'pending', got {step['status']!r}"
            )

    def test_completion_fraction_is_zero_in_initial_checkpoint(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """Initial checkpoint must have completion_fraction == 0.0."""
        uow_id = "uow_cp_file_003"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())
        assert data["completion_fraction"] == 0.0

    def test_next_step_index_is_zero_in_initial_checkpoint(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """Initial checkpoint must have next_step_index == 0."""
        uow_id = "uow_cp_file_004"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())
        assert data["next_step_index"] == 0

    def test_uow_id_is_recorded_in_initial_checkpoint(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """Initial checkpoint must record the correct uow_id."""
        uow_id = "uow_cp_file_005"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())
        assert data["uow_id"] == uow_id


# ---------------------------------------------------------------------------
# Test: canonical step plan in initial checkpoint
# ---------------------------------------------------------------------------

CANONICAL_STEP_NAMES = [
    "read_issue",
    "create_worktree",
    "implement",
    "run_tests",
    "open_pr",
    "write_result",
]


class TestCheckpointStepsMatchCanonicalPlan:
    def test_initial_checkpoint_has_six_canonical_steps(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """Initial checkpoint must have exactly 6 canonical steps."""
        uow_id = "uow_cp_steps_001"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())
        assert len(data["steps"]) == len(CANONICAL_STEP_NAMES)

    def test_canonical_step_names_match(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """Step names in the initial checkpoint must match the canonical plan."""
        uow_id = "uow_cp_steps_002"
        _insert_uow(db_path, uow_id)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        cp_file = isolated_checkpoints / uow_id / "checkpoint.json"
        data = json.loads(cp_file.read_text())
        actual_names = [s["name"] for s in data["steps"]]
        assert actual_names == CANONICAL_STEP_NAMES


# ---------------------------------------------------------------------------
# Test: CHECKPOINT_PATH injection in assembled subagent prompt
# ---------------------------------------------------------------------------

class TestPromptContainsCheckpointPath:
    def test_dispatcher_receives_checkpoint_path_line(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """The subagent prompt passed to the dispatcher must contain CHECKPOINT_PATH:."""
        uow_id = "uow_cp_prompt_001"
        _insert_uow(db_path, uow_id)

        received_prompts: list[str] = []

        def capturing_dispatcher(instructions: str, uid: str) -> str:
            received_prompts.append(instructions)
            return "task-captured"

        executor = Executor(registry, dispatcher=capturing_dispatcher)
        executor.execute_uow(uow_id)

        assert received_prompts, "Dispatcher was not called"
        prompt = received_prompts[0]
        assert "CHECKPOINT_PATH:" in prompt, (
            f"Expected 'CHECKPOINT_PATH:' in prompt, got:\n{prompt[:500]}"
        )

    def test_checkpoint_path_in_prompt_contains_uow_id(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """CHECKPOINT_PATH in the prompt must contain the UoW's uow_id."""
        uow_id = "uow_cp_prompt_002"
        _insert_uow(db_path, uow_id)

        received_prompts: list[str] = []

        def capturing_dispatcher(instructions: str, uid: str) -> str:
            received_prompts.append(instructions)
            return "task-captured"

        executor = Executor(registry, dispatcher=capturing_dispatcher)
        executor.execute_uow(uow_id)

        prompt = received_prompts[0]
        # Find the CHECKPOINT_PATH: line and verify uow_id is in the path value
        cp_lines = [line for line in prompt.splitlines() if line.startswith("CHECKPOINT_PATH:")]
        assert cp_lines, "No CHECKPOINT_PATH: line found in prompt"
        cp_line = cp_lines[0]
        assert uow_id in cp_line, (
            f"Expected uow_id={uow_id!r} in CHECKPOINT_PATH line: {cp_line!r}"
        )

    def test_checkpoint_path_in_prompt_ends_with_checkpoint_json(
        self,
        registry: Registry,
        db_path: Path,
        isolated_checkpoints: Path,
    ) -> None:
        """CHECKPOINT_PATH value in the prompt must end with 'checkpoint.json'."""
        uow_id = "uow_cp_prompt_003"
        _insert_uow(db_path, uow_id)

        received_prompts: list[str] = []

        def capturing_dispatcher(instructions: str, uid: str) -> str:
            received_prompts.append(instructions)
            return "task-captured"

        executor = Executor(registry, dispatcher=capturing_dispatcher)
        executor.execute_uow(uow_id)

        prompt = received_prompts[0]
        cp_lines = [line for line in prompt.splitlines() if line.startswith("CHECKPOINT_PATH:")]
        assert cp_lines
        cp_path = cp_lines[0].split("CHECKPOINT_PATH:", 1)[1].strip()
        assert cp_path.endswith("checkpoint.json"), (
            f"CHECKPOINT_PATH value should end with 'checkpoint.json', got: {cp_path!r}"
        )


# ---------------------------------------------------------------------------
# Test: _FUNCTIONAL_ENGINEER_PREAMBLE contains checkpoint protocol
# ---------------------------------------------------------------------------

class TestPreambleContainsCheckpointProtocol:
    def test_preamble_contains_checkpoint_protocol_section(self) -> None:
        """_FUNCTIONAL_ENGINEER_PREAMBLE must include '## Checkpoint Protocol'."""
        assert "## Checkpoint Protocol" in _FUNCTIONAL_ENGINEER_PREAMBLE

    def test_preamble_contains_canonical_step_plan(self) -> None:
        """_FUNCTIONAL_ENGINEER_PREAMBLE must mention each canonical step name."""
        for step_name in CANONICAL_STEP_NAMES:
            assert step_name in _FUNCTIONAL_ENGINEER_PREAMBLE, (
                f"Expected canonical step {step_name!r} in _FUNCTIONAL_ENGINEER_PREAMBLE"
            )

    def test_preamble_contains_atomic_write_instructions(self) -> None:
        """_FUNCTIONAL_ENGINEER_PREAMBLE must describe the tmp→rename write protocol."""
        assert ".tmp" in _FUNCTIONAL_ENGINEER_PREAMBLE, (
            "Expected atomic write protocol (.tmp) in _FUNCTIONAL_ENGINEER_PREAMBLE"
        )
        assert "rename" in _FUNCTIONAL_ENGINEER_PREAMBLE or "tmp.rename" in _FUNCTIONAL_ENGINEER_PREAMBLE, (
            "Expected 'rename' or 'tmp.rename' in _FUNCTIONAL_ENGINEER_PREAMBLE"
        )

    def test_preamble_references_checkpoint_path_variable(self) -> None:
        """_FUNCTIONAL_ENGINEER_PREAMBLE must reference CHECKPOINT_PATH."""
        assert "CHECKPOINT_PATH" in _FUNCTIONAL_ENGINEER_PREAMBLE

    def test_preamble_describes_write_protocol_steps(self) -> None:
        """_FUNCTIONAL_ENGINEER_PREAMBLE must describe before/after step write instructions."""
        assert "in_progress" in _FUNCTIONAL_ENGINEER_PREAMBLE
        assert "complete" in _FUNCTIONAL_ENGINEER_PREAMBLE
