"""
Tests for src/orchestration/checkpoint.py and startup_sweep._read_checkpoint.

Covers:
1. _read_checkpoint(None) → returns None
2. _read_checkpoint("/nonexistent/path") → returns None
3. _read_checkpoint(<path with malformed JSON>) → returns None
4. _read_checkpoint(<path with valid JSON>) → returns the dict
5. _write_checkpoint_atomic(<path>, <dict>) → file exists, content matches,
   no .tmp file left behind
6. CHECKPOINT_SCHEMA_VERSION constant is defined and equals 1
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing from src/ and scheduled-tasks/ in the worktree
# ---------------------------------------------------------------------------

_WORKTREE_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _WORKTREE_ROOT / "src"
_SCHEDULED_TASKS = _WORKTREE_ROOT / "scheduled-tasks"
for _p in (_SRC, _SCHEDULED_TASKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Import helpers under test
# ---------------------------------------------------------------------------

from orchestration.checkpoint import (  # noqa: E402
    CHECKPOINT_SCHEMA_VERSION,
    _write_checkpoint_atomic,
    write_checkpoint,
)

# startup_sweep._read_checkpoint has a different signature (takes a path string
# rather than a uow_id) so we import it explicitly to test the startup-sweep
# reader path.
from startup_sweep import _read_checkpoint as sweep_read_checkpoint  # noqa: E402


# ---------------------------------------------------------------------------
# Tests for startup_sweep._read_checkpoint(checkpoint_ref: str | None)
# ---------------------------------------------------------------------------


class TestSweepReadCheckpoint:
    """Tests for the startup_sweep._read_checkpoint path-based reader."""

    def test_none_input_returns_none(self):
        """_read_checkpoint(None) must return None without raising."""
        assert sweep_read_checkpoint(None) is None

    def test_nonexistent_path_returns_none(self, tmp_path: Path):
        """_read_checkpoint of a path that does not exist must return None."""
        missing = tmp_path / "no_such_file.json"
        assert sweep_read_checkpoint(str(missing)) is None

    def test_malformed_json_returns_none(self, tmp_path: Path):
        """_read_checkpoint of a file with invalid JSON must return None."""
        bad_json = tmp_path / "checkpoint.json"
        bad_json.write_text("{not valid json: }")
        assert sweep_read_checkpoint(str(bad_json)) is None

    def test_valid_json_returns_dict(self, tmp_path: Path):
        """_read_checkpoint of a valid checkpoint file returns the parsed dict."""
        data = {
            "uow_id": "uow_20260525_abc123",
            "checkpoint_version": 1,
            "written_at": "2026-05-25T12:34:56Z",
            "steps": [],
            "next_step_index": 2,
            "next_step_name": "implement",
            "completion_fraction": 0.4,
            "notes": "Context for fresh subagent.",
        }
        checkpoint_file = tmp_path / "checkpoint.json"
        checkpoint_file.write_text(json.dumps(data))
        result = sweep_read_checkpoint(str(checkpoint_file))
        assert result is not None
        assert result["uow_id"] == "uow_20260525_abc123"
        assert result["next_step_index"] == 2
        assert result["completion_fraction"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Tests for _write_checkpoint_atomic
# ---------------------------------------------------------------------------


class TestWriteCheckpointAtomic:
    """Tests for the atomic tmp→rename write helper."""

    def test_file_created_with_correct_content(self, tmp_path: Path):
        """After _write_checkpoint_atomic, the target file exists and contains the data."""
        target = tmp_path / "checkpoint.json"
        data = {
            "uow_id": "uow_test_001",
            "checkpoint_version": 1,
            "written_at": "2026-06-01T00:00:00Z",
            "steps": [{"index": 0, "name": "read_issue", "status": "complete",
                        "completed_at": "2026-06-01T00:01:00Z", "artifacts": {}}],
            "next_step_index": 1,
            "next_step_name": "create_worktree",
            "completion_fraction": 0.2,
            "notes": "",
        }
        _write_checkpoint_atomic(target, data)
        assert target.exists()
        written = json.loads(target.read_text())
        assert written == data

    def test_no_tmp_file_left_behind(self, tmp_path: Path):
        """After a successful atomic write, no .tmp file should remain."""
        target = tmp_path / "checkpoint.json"
        data = {"uow_id": "uow_test_002", "checkpoint_version": 1}
        _write_checkpoint_atomic(target, data)
        tmp_file = target.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_creates_parent_directories(self, tmp_path: Path):
        """_write_checkpoint_atomic creates missing parent directories."""
        target = tmp_path / "subdir" / "nested" / "checkpoint.json"
        data = {"uow_id": "uow_test_003"}
        _write_checkpoint_atomic(target, data)
        assert target.exists()

    def test_overwrites_existing_file(self, tmp_path: Path):
        """_write_checkpoint_atomic overwrites an existing checkpoint file."""
        target = tmp_path / "checkpoint.json"
        target.write_text(json.dumps({"uow_id": "old"}))
        new_data = {"uow_id": "uow_test_004", "checkpoint_version": 1}
        _write_checkpoint_atomic(target, new_data)
        written = json.loads(target.read_text())
        assert written["uow_id"] == "uow_test_004"


# ---------------------------------------------------------------------------
# Tests for CHECKPOINT_SCHEMA_VERSION constant
# ---------------------------------------------------------------------------


class TestCheckpointSchemaVersion:
    """Verify the schema version constant is defined and correct."""

    def test_schema_version_is_1(self):
        """CHECKPOINT_SCHEMA_VERSION must equal 1."""
        assert CHECKPOINT_SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Tests for write_checkpoint — subagent_session_id field
# ---------------------------------------------------------------------------


class TestWriteCheckpointSubagentSessionId:
    """Verify write_checkpoint writes subagent_session_id into the checkpoint file."""

    def test_subagent_session_id_written_to_file(self, tmp_path: Path, monkeypatch):
        """write_checkpoint must include subagent_session_id in the checkpoint JSON."""
        import src.orchestration.paths as paths_mod

        monkeypatch.setattr(
            paths_mod,
            "CHECKPOINTS_DIR",
            tmp_path,
        )
        # Re-import checkpoint_path after monkeypatching so it picks up the patched dir.
        import importlib
        import orchestration.checkpoint as cp_mod
        importlib.reload(cp_mod)

        session_id = "test-session-uuid-1234"
        path = cp_mod.write_checkpoint(
            uow_id="uow_test_session",
            steps=[],
            next_step_index=0,
            subagent_session_id=session_id,
        )
        written = json.loads(path.read_text())
        assert written["subagent_session_id"] == session_id
