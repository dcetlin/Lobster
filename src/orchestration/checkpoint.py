"""
Executor checkpoint protocol — disk schema and atomic write helpers.

Checkpoint files live at:
    ~/lobster-workspace/orchestration/checkpoints/{uow_id}/checkpoint.json

Schema: {"step_name": str, "status": str, "artifacts": dict, "written_at": str (ISO-8601 UTC)}

Atomic write: write to .tmp, then os.replace().
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from src.orchestration.paths import CHECKPOINTS_DIR


class CheckpointData(TypedDict):
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
    """Write checkpoint atomically. Returns the path written."""
    target = checkpoint_path(uow_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    data: CheckpointData = {
        "step_name": step_name,
        "status": status,
        "artifacts": artifacts or {},
        "written_at": datetime.now(timezone.utc).isoformat(),
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
