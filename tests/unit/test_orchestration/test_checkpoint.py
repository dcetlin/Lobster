"""
Tests for src/orchestration/checkpoint.py — disk schema and atomic write helpers.

Covers:
1. write_checkpoint creates file at correct path with correct schema keys
2. _read_checkpoint returns None when file does not exist
3. _read_checkpoint returns the written data after write_checkpoint
4. write_checkpoint is atomic: second call replaces (not appends)
5. write_checkpoint with empty steps stores [] (not null)
6. Schema compatibility: next_step_index and steps[] are readable by startup_sweep reader
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.orchestration.checkpoint as checkpoint_module
from src.orchestration.checkpoint import write_checkpoint, _read_checkpoint, checkpoint_path


@pytest.fixture()
def tmp_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CHECKPOINTS_DIR to a temp directory for each test."""
    checkpoints_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoint_module, "CHECKPOINTS_DIR", checkpoints_dir)
    return checkpoints_dir


def _make_step(index: int, name: str, status: str = "complete", artifacts: dict | None = None) -> dict:
    """Helper to build a StepRecord-compatible dict."""
    return {
        "index": index,
        "name": name,
        "status": status,
        "completed_at": "2026-01-01T00:00:00+00:00",
        "artifacts": artifacts or {},
    }


# ---------------------------------------------------------------------------
# 1. write_checkpoint creates file at correct path with correct schema keys
# ---------------------------------------------------------------------------

class TestWriteCheckpointSchema:
    def test_file_created_at_correct_path(self, tmp_checkpoints: Path) -> None:
        result = write_checkpoint("uow_abc123", steps=[], next_step_index=1)
        expected = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        assert result == expected
        assert expected.exists()

    def test_schema_keys_present(self, tmp_checkpoints: Path) -> None:
        steps = [_make_step(0, "step1", artifacts={"pr": 42})]
        write_checkpoint("uow_abc123", steps=steps, next_step_index=1)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert set(data.keys()) == {
            "uow_id", "checkpoint_version", "written_at",
            "steps", "next_step_index", "next_step_name",
            "completion_fraction", "notes",
        }

    def test_uow_id_written(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", steps=[], next_step_index=0)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["uow_id"] == "uow_abc123"

    def test_checkpoint_version_is_1(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", steps=[], next_step_index=0)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["checkpoint_version"] == 1

    def test_next_step_index_written(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", steps=[], next_step_index=3)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["next_step_index"] == 3

    def test_steps_array_written(self, tmp_checkpoints: Path) -> None:
        steps = [
            _make_step(0, "impl", artifacts={"pr": 99}),
            _make_step(1, "tests", status="in_progress"),
        ]
        write_checkpoint("uow_abc123", steps=steps, next_step_index=1)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert len(data["steps"]) == 2
        assert data["steps"][0]["name"] == "impl"
        assert data["steps"][0]["artifacts"] == {"pr": 99}

    def test_written_at_is_iso_timestamp(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", steps=[], next_step_index=0)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert "T" in data["written_at"]  # ISO-8601 format
        assert "+" in data["written_at"] or "Z" in data["written_at"] or data["written_at"].endswith("+00:00")

    def test_optional_fields_default(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", steps=[], next_step_index=0)
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["next_step_name"] == ""
        assert data["completion_fraction"] == 0.0
        assert data["notes"] == ""

    def test_optional_fields_set(self, tmp_checkpoints: Path) -> None:
        write_checkpoint(
            "uow_abc123",
            steps=[],
            next_step_index=2,
            next_step_name="finalize",
            completion_fraction=0.5,
            notes="halfway done",
        )
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["next_step_name"] == "finalize"
        assert data["completion_fraction"] == 0.5
        assert data["notes"] == "halfway done"


# ---------------------------------------------------------------------------
# 2. _read_checkpoint returns None when file does not exist
# ---------------------------------------------------------------------------

class TestReadCheckpointMissing:
    def test_returns_none_for_nonexistent_uow(self, tmp_checkpoints: Path) -> None:
        result = _read_checkpoint("uow_nonexistent")
        assert result is None

    def test_returns_none_when_dir_missing(self, tmp_checkpoints: Path) -> None:
        # No directory created at all
        result = _read_checkpoint("uow_missing_dir")
        assert result is None


# ---------------------------------------------------------------------------
# 3. _read_checkpoint returns the written data after write_checkpoint
# ---------------------------------------------------------------------------

class TestReadCheckpointRoundtrip:
    def test_read_returns_written_data(self, tmp_checkpoints: Path) -> None:
        steps = [_make_step(0, "post_impl", artifacts={"pr_url": "https://github.com/x/y/pull/1"})]
        write_checkpoint("uow_xyz", steps=steps, next_step_index=1)
        data = _read_checkpoint("uow_xyz")
        assert data is not None
        assert data["uow_id"] == "uow_xyz"
        assert data["next_step_index"] == 1
        assert len(data["steps"]) == 1
        assert data["steps"][0]["name"] == "post_impl"
        assert data["steps"][0]["artifacts"] == {"pr_url": "https://github.com/x/y/pull/1"}

    def test_read_preserves_all_fields(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_xyz", steps=[], next_step_index=0)
        data = _read_checkpoint("uow_xyz")
        assert data is not None
        assert "written_at" in data
        assert "checkpoint_version" in data


# ---------------------------------------------------------------------------
# 4. write_checkpoint is atomic: second call replaces (not appends)
# ---------------------------------------------------------------------------

class TestWriteCheckpointReplaces:
    def test_second_write_replaces_first(self, tmp_checkpoints: Path) -> None:
        step1 = _make_step(0, "step1", artifacts={"a": 1})
        write_checkpoint("uow_replace", steps=[step1], next_step_index=1)
        step2 = _make_step(0, "step1", artifacts={"a": 1})
        write_checkpoint("uow_replace", steps=[step2], next_step_index=2, completion_fraction=0.5)
        data = _read_checkpoint("uow_replace")
        assert data is not None
        assert data["next_step_index"] == 2
        assert data["completion_fraction"] == 0.5

    def test_no_residual_tmp_file_after_write(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_replace", steps=[], next_step_index=0)
        tmp_file = tmp_checkpoints / "uow_replace" / "checkpoint.tmp"
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# 5. write_checkpoint with empty steps stores [] (not null)
# ---------------------------------------------------------------------------

class TestWriteCheckpointEmptySteps:
    def test_empty_steps_stored_as_list(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_nosteps", steps=[], next_step_index=0)
        data = _read_checkpoint("uow_nosteps")
        assert data is not None
        assert data["steps"] == []
        assert isinstance(data["steps"], list)


# ---------------------------------------------------------------------------
# 6. Schema compatibility: next_step_index and steps[] readable by startup_sweep
# ---------------------------------------------------------------------------

class TestStartupSweepCompatibility:
    def test_next_step_index_nonzero_triggers_checkpoint_classification(
        self, tmp_checkpoints: Path
    ) -> None:
        """A checkpoint with next_step_index > 0 satisfies the startup_sweep
        classification condition: checkpoint.get("next_step_index", 0) > 0."""
        step = _make_step(0, "impl", artifacts={"branch": "feat/my-feature"})
        write_checkpoint("uow_compat", steps=[step], next_step_index=1)
        data = _read_checkpoint("uow_compat")
        assert data is not None
        assert data.get("next_step_index", 0) > 0

    def test_steps_array_iterable_by_startup_sweep(self, tmp_checkpoints: Path) -> None:
        """startup_sweep iterates steps: for step in checkpoint_data.get('steps', [])."""
        steps = [
            _make_step(0, "impl", artifacts={"worktree_path": "/tmp/fake"}),
            _make_step(1, "tests", status="in_progress"),
        ]
        write_checkpoint("uow_compat", steps=steps, next_step_index=2)
        data = _read_checkpoint("uow_compat")
        assert data is not None
        collected_steps = data.get("steps", [])
        assert len(collected_steps) == 2
        # Step 0 is complete and has artifacts
        assert collected_steps[0]["status"] == "complete"
        assert "worktree_path" in collected_steps[0]["artifacts"]
