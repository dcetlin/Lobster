"""
Tests for src/orchestration/checkpoint.py — disk schema and atomic write helpers.

Covers:
1. write_checkpoint creates file at correct path with correct schema keys
2. _read_checkpoint returns None when file does not exist
3. _read_checkpoint returns the written data after write_checkpoint
4. write_checkpoint is atomic: second call replaces (not appends)
5. write_checkpoint with artifacts=None stores {} (not null)
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


# ---------------------------------------------------------------------------
# 1. write_checkpoint creates file at correct path with correct schema keys
# ---------------------------------------------------------------------------

class TestWriteCheckpointSchema:
    def test_file_created_at_correct_path(self, tmp_checkpoints: Path) -> None:
        result = write_checkpoint("uow_abc123", "step1", "in_progress")
        expected = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        assert result == expected
        assert expected.exists()

    def test_schema_keys_present(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", "step1", "in_progress", artifacts={"pr": 42})
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert set(data.keys()) == {"step_name", "status", "artifacts", "written_at"}

    def test_step_name_and_status_written(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", "my_step", "done")
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["step_name"] == "my_step"
        assert data["status"] == "done"

    def test_artifacts_written(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", "step1", "in_progress", artifacts={"pr": 99, "file": "foo.py"})
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert data["artifacts"] == {"pr": 99, "file": "foo.py"}

    def test_written_at_is_iso_timestamp(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_abc123", "step1", "in_progress")
        path = tmp_checkpoints / "uow_abc123" / "checkpoint.json"
        data = json.loads(path.read_text())
        assert "T" in data["written_at"]  # ISO-8601 format
        assert "+" in data["written_at"] or "Z" in data["written_at"] or data["written_at"].endswith("+00:00")


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
        write_checkpoint("uow_xyz", "post_impl", "complete", artifacts={"pr_url": "https://github.com/x/y/pull/1"})
        data = _read_checkpoint("uow_xyz")
        assert data is not None
        assert data["step_name"] == "post_impl"
        assert data["status"] == "complete"
        assert data["artifacts"] == {"pr_url": "https://github.com/x/y/pull/1"}

    def test_read_preserves_all_fields(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_xyz", "startup", "running")
        data = _read_checkpoint("uow_xyz")
        assert data is not None
        assert "written_at" in data


# ---------------------------------------------------------------------------
# 4. write_checkpoint is atomic: second call replaces (not appends)
# ---------------------------------------------------------------------------

class TestWriteCheckpointReplaces:
    def test_second_write_replaces_first(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_replace", "step1", "in_progress", artifacts={"a": 1})
        write_checkpoint("uow_replace", "step2", "complete", artifacts={"b": 2})
        data = _read_checkpoint("uow_replace")
        assert data is not None
        assert data["step_name"] == "step2"
        assert data["status"] == "complete"
        assert data["artifacts"] == {"b": 2}

    def test_no_residual_tmp_file_after_write(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_replace", "step1", "in_progress")
        tmp_file = tmp_checkpoints / "uow_replace" / "checkpoint.tmp"
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# 5. write_checkpoint with artifacts=None stores {} (not null)
# ---------------------------------------------------------------------------

class TestWriteCheckpointNullArtifacts:
    def test_none_artifacts_stored_as_empty_dict(self, tmp_checkpoints: Path) -> None:
        write_checkpoint("uow_noartifacts", "step1", "done", artifacts=None)
        data = _read_checkpoint("uow_noartifacts")
        assert data is not None
        assert data["artifacts"] == {}
        # Confirm it is actually a dict, not None/null
        assert isinstance(data["artifacts"], dict)

    def test_default_artifacts_stored_as_empty_dict(self, tmp_checkpoints: Path) -> None:
        # Calling without artifacts= kwarg uses the default (None -> {})
        write_checkpoint("uow_default", "step1", "done")
        data = _read_checkpoint("uow_default")
        assert data is not None
        assert data["artifacts"] == {}
