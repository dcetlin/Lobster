"""
Executor checkpoint protocol — disk schema and atomic write helpers.

Checkpoint files live at:
    ~/lobster-workspace/orchestration/checkpoints/{uow_id}/checkpoint.json

Schema (full spec, compatible with startup_sweep._read_checkpoint reader):
{
  "uow_id": str,
  "checkpoint_version": 1,
  "written_at": str (ISO-8601 UTC),
  "steps": [{"index": int, "name": str, "status": str, "completed_at": str, "artifacts": dict}],
  "next_step_index": int,
  "next_step_name": str,
  "completion_fraction": float,
  "notes": str
}

Atomic write: write to .tmp, then os.replace().
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from src.orchestration.paths import CHECKPOINTS_DIR


class StepRecord(TypedDict):
    index: int
    name: str
    status: str
    completed_at: str
    artifacts: dict


class CheckpointData(TypedDict):
    uow_id: str
    checkpoint_version: int
    written_at: str
    steps: list[StepRecord]
    next_step_index: int
    next_step_name: str
    completion_fraction: float
    notes: str


def checkpoint_path(uow_id: str) -> Path:
    return CHECKPOINTS_DIR / uow_id / "checkpoint.json"


def write_checkpoint(
    uow_id: str,
    steps: list[StepRecord],
    next_step_index: int,
    next_step_name: str = "",
    completion_fraction: float = 0.0,
    notes: str = "",
) -> Path:
    """Write checkpoint atomically. Returns the path written.

    Produces a full spec schema compatible with the startup_sweep
    _read_checkpoint() reader. A next_step_index > 0 is required for
    startup_sweep to classify the orphan as
    'orphan_kill_during_execution_with_checkpoint' and surface resume
    context to the Steward.

    Args:
        uow_id: The UoW identifier.
        steps: List of step records. Each completed step should have
            status='complete', a non-empty completed_at, and an artifacts dict.
        next_step_index: Index of the next step to execute (0-based).
        next_step_name: Name of the next step (human-readable, for audit notes).
        completion_fraction: Fraction of work complete (0.0–1.0).
        notes: Free-form notes for the Steward (e.g. last known state).
    """
    target = checkpoint_path(uow_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    data: CheckpointData = {
        "uow_id": uow_id,
        "checkpoint_version": 1,
        "written_at": now_iso,
        "steps": steps,
        "next_step_index": next_step_index,
        "next_step_name": next_step_name,
        "completion_fraction": completion_fraction,
        "notes": notes,
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, target)
    return target


def _read_checkpoint(uow_id: str) -> CheckpointData | None:
    """Return the checkpoint for uow_id, or None if none exists."""
    path = checkpoint_path(uow_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
