"""
prescribe_artifacts — write and read wos_prescribe artifact files.

The wos_prescribe inbox message historically embedded large fields
(issue_body, steward_log, dan_register, vision_orientation) directly in the
JSON payload, creating messages up to 98 KB.  When a batch of these messages
landed in one wait_for_messages call, the dispatcher built huge subagent
prompts and the resulting API inference round-trip exceeded 10 minutes,
causing the heartbeat to go stale and triggering a health-daemon restart.

This module solves the problem at the seam where large payload diverges from
small transport:

  * ``write_prescribe_artifact`` — called by the steward at message-emit time.
    Writes a JSON artifact file keyed by uow_id under the prescribe-artifacts
    directory.  Returns the artifact file path.

  * ``read_prescribe_artifact`` — called by the dispatcher handler at
    consumption time.  Reads the artifact file and returns its contents as a
    dict.  Returns None when the file does not exist (backward-compat path for
    in-flight messages that carry large fields inline).

The inbox message retains only the scalar fields required for routing
(uow_id, short summary, artifact_path) plus small scalars that are always
needed (reentry_posture, completion_gap, cycles, new_cycles, executor_type,
prescribed_skills, now_iso, uow_source, uow_type, success_criteria).  The
large blobs (issue_body, steward_log, dan_register, vision_orientation,
diagnosis_section) are written to the artifact file only.

Public API
----------
``PRESCRIBE_ARTIFACTS_DIR`` — module-level Path constant; tests may monkeypatch.
``write_prescribe_artifact(uow_id, artifact_dir=None, **fields) -> Path``
``read_prescribe_artifact(artifact_path, artifact_dir=None) -> dict | None``
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constant — tests monkeypatch this to avoid writing to real dirs.
# ---------------------------------------------------------------------------

PRESCRIBE_ARTIFACTS_DIR: Path = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "prescribe-artifacts"

# Large fields written to the artifact file rather than the inbox message.
# These are the fields that drive message size above ~1 KB per UoW.
LARGE_FIELDS: frozenset[str] = frozenset({
    "issue_body",
    "steward_log",
    "dan_register",
    "vision_orientation",
    "diagnosis_section",
})


def write_prescribe_artifact(
    uow_id: str,
    *,
    artifact_dir: Path | None = None,
    issue_body: str = "",
    steward_log: str = "",
    dan_register: str = "",
    vision_orientation: str = "",
    diagnosis_section: dict[str, Any] | None = None,
) -> Path:
    """
    Write a prescribe artifact file for *uow_id* and return its path.

    The artifact file is a JSON document containing the large fields that
    are stripped from the inbox message.  It is keyed by ``uow_id`` so the
    dispatcher can locate it without any additional state.

    Args:
        uow_id: The Unit of Work ID.
        artifact_dir: Override for the artifacts directory.  Defaults to
            ``PRESCRIBE_ARTIFACTS_DIR``.  Passing a value in tests keeps
            writes out of the real workspace.
        issue_body: Raw GitHub issue body text.
        steward_log: JSONL steward log string.
        dan_register: Dan's developmental register excerpt.
        vision_orientation: Vision context string.
        diagnosis_section: Dict from _build_diagnosis_section.

    Returns:
        The Path to the written artifact file.
    """
    resolved_dir = Path(artifact_dir) if artifact_dir is not None else PRESCRIBE_ARTIFACTS_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = resolved_dir / f"{uow_id}.prescribe.json"
    payload: dict[str, Any] = {
        "uow_id": uow_id,
        "issue_body": issue_body,
        "steward_log": steward_log,
        "dan_register": dan_register,
        "vision_orientation": vision_orientation,
        "diagnosis_section": diagnosis_section if diagnosis_section is not None else {},
    }
    artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return artifact_path


def read_prescribe_artifact(
    artifact_path: str | Path,
    *,
    artifact_dir: Path | None = None,
) -> dict[str, Any] | None:
    """
    Read a prescribe artifact file and return its contents, or None if missing.

    A missing file is not an error — it means the inbox message was written
    before this migration was deployed (backward-compat for in-flight messages
    that still carry large fields inline in the message dict).

    Args:
        artifact_path: Absolute path to the artifact file, as stored in the
            inbox message's ``artifact_path`` field.
        artifact_dir: Unused; accepted for symmetry with ``write_prescribe_artifact``
            and to allow future relocation of the artifact directory without
            changing call sites.

    Returns:
        The parsed artifact dict, or None if the file does not exist or cannot
        be read (parse errors are re-raised since they indicate a corrupt write).
    """
    resolved = Path(artifact_path)
    if not resolved.exists():
        return None
    return json.loads(resolved.read_text(encoding="utf-8"))
