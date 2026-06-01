"""
Executor checkpoint protocol — disk schema and atomic write helpers.

Checkpoint files live at:
    ~/lobster-workspace/orchestration/checkpoints/{uow_id}/checkpoint.json

Schema written by write_checkpoint() (per-step helper):
    {"step_name": str, "status": str, "artifacts": dict, "written_at": str (ISO-8601 UTC)}

NOTE: write_checkpoint() is a simplified per-step helper that records a single
step's progress. It does NOT produce the full startup-sweep-compatible checkpoint
schema (which requires next_step_index, steps[], completion_fraction, etc.).
See Issue #1323 for the full-schema writer that subagents will use.

Atomic write: write to .tmp, then os.replace().
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from src.orchestration.paths import CHECKPOINTS_DIR


class StepCheckpointData(TypedDict):
    """Per-step checkpoint record written by write_checkpoint().

    This is a simplified schema capturing a single step's state — NOT the full
    checkpoint schema that the startup sweep's _read_checkpoint() reader expects
    (which requires next_step_index, steps[], completion_fraction, etc.).

    TODO(#1323): Replace with the full schema writer once Issue #1323 ships.
    """
    step_name: str
    status: str
    artifacts: dict
    written_at: str


def checkpoint_path(uow_id: str) -> Path:
    return CHECKPOINTS_DIR / uow_id / "checkpoint.json"


def write_checkpoint(
    uow_id: str,
    step_name: str,
    status: str,
    artifacts: dict | None = None,
) -> Path:
    """Write a per-step checkpoint atomically. Returns the path written.

    IMPORTANT: This is a simplified helper — the file it produces contains only
    {step_name, status, artifacts, written_at}. It is NOT compatible with the
    startup sweep's orphan-classification reader, which expects next_step_index
    and a steps[] array. Files written here will NOT trigger the
    'orphan_kill_during_execution_with_checkpoint' classification.

    This function is intended as scaffolding for Issue #1323, which will
    introduce the full-schema writer that subagents call at step boundaries.
    Until #1323 ships, do not treat write_checkpoint() output as resumable
    checkpoint data.

    See also: StepCheckpointData (the TypedDict for this simplified schema).
    TODO(#1323): Replace callers with the full-schema writer once available.
    """
    target = checkpoint_path(uow_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    data: StepCheckpointData = {
        "step_name": step_name,
        "status": status,
        "artifacts": artifacts or {},
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, target)
    return target


def _read_checkpoint(uow_id: str) -> StepCheckpointData | None:
    """Return the checkpoint for uow_id, or None if none exists."""
    path = checkpoint_path(uow_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
