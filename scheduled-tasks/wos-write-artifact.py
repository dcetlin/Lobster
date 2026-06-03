#!/usr/bin/env python3
"""
WOS Write Artifact — writes a WorkflowArtifact and transitions a UoW.

Called by the prescription subagent after it generates a prescription directly.
This script handles the deterministic side-effects (artifact write + registry
transition) so the subagent only needs to provide the prescription text.

Usage:
    uv run scheduled-tasks/wos-write-artifact.py \\
        --uow-id <uow_id> \\
        --new-cycles <int> \\
        --executor-type <str> \\
        --prescribed-skills <json-array-str> \\
        < prescription_text.txt

The prescription text is read from stdin.

Exit codes:
    0: WorkflowArtifact written and UoW transitioned to ready-for-executor.
    1: Failure — UoW reset to ready-for-steward if possible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-write-artifact")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _reset_to_ready_for_steward(uow_id: str) -> None:
    try:
        from orchestration.registry import Registry, UoWStatus
        registry = Registry()
        rows = registry.transition(uow_id, UoWStatus.READY_FOR_STEWARD, UoWStatus.PRESCRIBING)
        if rows == 1:
            log.info("wos-write-artifact: reset UoW %s to ready-for-steward", uow_id)
        else:
            log.warning(
                "wos-write-artifact: reset returned 0 rows for %s — already advanced",
                uow_id,
            )
    except Exception as exc:
        log.error(
            "wos-write-artifact: reset failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="WOS Write Artifact")
    parser.add_argument("--uow-id", required=True, help="Unit of Work ID")
    parser.add_argument("--new-cycles", type=int, default=1, help="New steward_cycles value")
    parser.add_argument(
        "--executor-type",
        default="functional-engineer",
        help="Executor type string",
    )
    parser.add_argument(
        "--prescribed-skills",
        default="[]",
        help="JSON array of prescribed skill names",
    )
    args = parser.parse_args()

    uow_id = args.uow_id
    new_cycles = args.new_cycles
    selected_executor_type = args.executor_type

    try:
        prescribed_skills = json.loads(args.prescribed_skills)
    except json.JSONDecodeError as exc:
        log.error("wos-write-artifact: invalid --prescribed-skills JSON: %s", exc)
        return 1

    # Read prescription text from stdin.
    prescription_text = sys.stdin.read().strip()
    if not prescription_text:
        log.error("wos-write-artifact: no prescription text on stdin")
        _reset_to_ready_for_steward(uow_id)
        return 1

    log.info("wos-write-artifact: processing prescription for UoW %s", uow_id)

    # Verify the UoW is still in prescribing state.
    try:
        from orchestration.registry import Registry, UoWStatus
        registry = Registry()
        uows = registry.query(status=UoWStatus.PRESCRIBING)
        uow = next((u for u in uows if u.id == uow_id), None)
        if uow is None:
            log.warning(
                "wos-write-artifact: UoW %s no longer in prescribing state — skipping",
                uow_id,
            )
            return 0
    except Exception as exc:
        log.error(
            "wos-write-artifact: failed to verify UoW %s state — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        return 1

    # Parse prescription text.
    try:
        from orchestration.steward import (
            _parse_workflow_artifact,
            _write_workflow_artifact,
            _write_steward_fields,
        )
        parsed = _parse_workflow_artifact(prescription_text)
    except (ImportError, ValueError) as exc:
        log.error(
            "wos-write-artifact: failed to parse prescription for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        _reset_to_ready_for_steward(uow_id)
        return 1

    instructions = parsed["instructions"]
    success_check = parsed.get("success_criteria_check", "")
    if success_check:
        instructions = instructions.rstrip() + f"\n\nCompletion check: {success_check}"

    if not instructions:
        log.error("wos-write-artifact: empty instructions for %s — resetting", uow_id)
        _reset_to_ready_for_steward(uow_id)
        return 1

    # Write WorkflowArtifact to disk.
    artifact_dir = Path(os.path.expanduser("~/lobster-workspace/orchestration/artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    try:
        artifact_path = _write_workflow_artifact(
            uow_id=uow_id,
            instructions=instructions,
            prescribed_skills=prescribed_skills,
            artifact_dir=artifact_dir,
            executor_type=selected_executor_type,
        )
        log.info("wos-write-artifact: WorkflowArtifact written at %s", artifact_path)
    except Exception as exc:
        log.error(
            "wos-write-artifact: _write_workflow_artifact failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        _reset_to_ready_for_steward(uow_id)
        return 1

    # Write steward fields.
    try:
        _write_steward_fields(
            registry, uow_id,
            workflow_artifact=artifact_path,
            prescribed_skills=json.dumps(prescribed_skills),
            steward_cycles=new_cycles,
        )
    except Exception as exc:
        log.warning(
            "wos-write-artifact: _write_steward_fields partially failed for %s — %s: %s "
            "(continuing with transition)",
            uow_id, type(exc).__name__, exc,
        )

    # Write audit log entry.
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        registry.append_audit_log(uow_id, {
            "event": "steward_prescription",
            "actor": "prescription-subagent",
            "uow_id": uow_id,
            "steward_cycles": new_cycles,
            "workflow_primitive": selected_executor_type,
            "prescribed_skills": prescribed_skills,
            "prescription_source": "direct_llm",
            "instructions_preview": instructions[:80],
            "prescription_path": "direct_subagent",
            "boundary_present": "Boundary:" in instructions,
            "timestamp": now_iso,
        })
    except Exception as audit_exc:
        log.warning(
            "wos-write-artifact: audit_log write failed for %s — %s: %s",
            uow_id, type(audit_exc).__name__, audit_exc,
        )

    # Transition prescribing → ready-for-executor.
    try:
        rows = registry.transition(uow_id, UoWStatus.READY_FOR_EXECUTOR, UoWStatus.PRESCRIBING)
        if rows == 0:
            log.warning(
                "wos-write-artifact: transition race for %s — "
                "another process may have already advanced this UoW",
                uow_id,
            )
            return 0
        log.info(
            "wos-write-artifact: UoW %s transitioned to ready-for-executor (cycles=%d)",
            uow_id, new_cycles,
        )
    except Exception as exc:
        log.error(
            "wos-write-artifact: ready-for-executor transition failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
