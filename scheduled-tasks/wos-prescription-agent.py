#!/usr/bin/env python3
"""
WOS Prescription Agent — async LLM prescription runner.

This script is invoked by a Lobster subagent when the dispatcher routes a
``wos_prescribe`` inbox message. It runs the blocking claude -p LLM call
(which can take up to LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS seconds) in a
background context, then writes the WorkflowArtifact and transitions the UoW
from ``prescribing`` → ``ready-for-executor``.

This is the async counterpart to steward._llm_prescribe. The LLM call is
identical; only the execution context changes — from a cron-spawned 3-minute
heartbeat to a background subagent that can run for as long as needed.

Usage:
    uv run scheduled-tasks/wos-prescription-agent.py --payload-stdin <<EOF
    <wos_prescribe JSON payload>
    EOF

    uv run scheduled-tasks/wos-prescription-agent.py --payload-file /path/to/payload.json

Exit codes:
    0: Prescription written, UoW transitioned to ready-for-executor.
    1: LLM call failed or UoW not in prescribing state — transition reset to
       ready-for-steward if possible.
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
log = logging.getLogger("wos-prescription-agent")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _reset_to_ready_for_steward(uow_id: str) -> None:
    """Transition prescribing → ready-for-steward as an error recovery step."""
    try:
        from orchestration.registry import Registry, UoWStatus
        registry = Registry()
        rows = registry.transition(uow_id, UoWStatus.READY_FOR_STEWARD, UoWStatus.PRESCRIBING)
        if rows == 1:
            log.info("prescription-agent: reset UoW %s to ready-for-steward", uow_id)
        else:
            log.warning(
                "prescription-agent: reset transition for %s returned 0 rows — "
                "already advanced by another process",
                uow_id,
            )
    except Exception as exc:
        log.error(
            "prescription-agent: reset_to_ready_for_steward failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="WOS Prescription Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--payload-stdin",
        action="store_true",
        help="Read wos_prescribe JSON payload from stdin",
    )
    group.add_argument(
        "--payload-file",
        metavar="PATH",
        help="Read wos_prescribe JSON payload from file",
    )
    args = parser.parse_args()

    # Load payload
    if args.payload_stdin:
        raw = sys.stdin.read()
    else:
        raw = Path(args.payload_file).read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("prescription-agent: failed to parse payload JSON — %s", exc)
        return 1

    uow_id: str = payload.get("uow_id", "")
    if not uow_id:
        log.error("prescription-agent: payload missing uow_id")
        return 1

    log.info("prescription-agent: starting prescription for UoW %s", uow_id)

    # Verify the UoW is still in prescribing state before doing work.
    try:
        from orchestration.registry import Registry, UoWStatus
        registry = Registry()
        uows = registry.query(status=UoWStatus.PRESCRIBING)
        uow = next((u for u in uows if u.id == uow_id), None)
        if uow is None:
            log.warning(
                "prescription-agent: UoW %s is no longer in prescribing state — "
                "another agent may have already processed it",
                uow_id,
            )
            return 0
    except Exception as exc:
        log.error(
            "prescription-agent: failed to verify UoW %s state — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        return 1

    # Run the LLM prescription via the steward's _llm_prescribe function.
    try:
        from orchestration.steward import (
            _llm_prescribe,
            _write_workflow_artifact,
            generate_v2_prescription,
            _write_steward_fields,
            LLMPrescriptionError,
        )
    except ImportError as exc:
        log.error(
            "prescription-agent: failed to import steward modules — %s: %s",
            type(exc).__name__, exc,
        )
        _reset_to_ready_for_steward(uow_id)
        return 1

    reentry_posture: str = payload.get("reentry_posture", "first_execution")
    completion_gap: str = payload.get("completion_gap", "")
    issue_body: str = payload.get("issue_body", "")
    selected_executor_type: str = payload.get("selected_executor_type", "functional-engineer")
    prescribed_skills: list = payload.get("prescribed_skills", [])
    new_cycles: int = payload.get("new_cycles", 1)

    # Step 1: Run LLM prescription.
    log.info(
        "prescription-agent: calling _llm_prescribe for %s "
        "(reentry_posture=%r, cycles=%d)",
        uow_id, reentry_posture, payload.get("cycles", 0),
    )
    llm_result = _llm_prescribe(
        uow=uow,
        reentry_posture=reentry_posture,
        completion_gap=completion_gap,
        issue_body=issue_body,
    )

    if llm_result is None:
        log.error(
            "prescription-agent: _llm_prescribe returned None for %s — "
            "resetting to ready-for-steward",
            uow_id,
        )
        _reset_to_ready_for_steward(uow_id)
        return 1

    instructions = llm_result.instructions
    success_check = llm_result.success_criteria_check
    if success_check:
        instructions = instructions.rstrip() + f"\n\nCompletion check: {success_check}"

    log.info(
        "prescription-agent: LLM prescription ready for %s "
        "(estimated_cycles=%d, boundary_present=%s)",
        uow_id, llm_result.estimated_cycles, llm_result.boundary_present,
    )

    # Step 2: Write WorkflowArtifact to disk.
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
        log.info(
            "prescription-agent: WorkflowArtifact written for %s at %s",
            uow_id, artifact_path,
        )
    except Exception as exc:
        log.error(
            "prescription-agent: _write_workflow_artifact failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        _reset_to_ready_for_steward(uow_id)
        return 1

    # Step 3: Write steward fields (workflow_artifact path, steward_cycles, prescribed_skills).
    try:
        _write_steward_fields(
            registry, uow_id,
            workflow_artifact=artifact_path,
            prescribed_skills=json.dumps(prescribed_skills),
            steward_cycles=new_cycles,
        )
    except Exception as exc:
        log.warning(
            "prescription-agent: _write_steward_fields partially failed for %s — %s: %s "
            "(continuing with transition)",
            uow_id, type(exc).__name__, exc,
        )

    # Write prescription audit entry.
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        registry.append_audit_log(uow_id, {
            "event": "steward_prescription",
            "actor": "prescription-agent",
            "uow_id": uow_id,
            "steward_cycles": new_cycles,
            "workflow_primitive": selected_executor_type,
            "prescribed_skills": prescribed_skills,
            "prescription_source": "async_llm",
            "instructions_preview": instructions[:80],
            "prescription_path": "async_inbox",
            "boundary_present": "Boundary:" in instructions,
            "timestamp": now_iso,
        })
    except Exception as audit_exc:
        log.warning(
            "prescription-agent: audit_log write failed for %s — %s: %s",
            uow_id, type(audit_exc).__name__, audit_exc,
        )

    # Step 5: Transition prescribing → ready-for-executor.
    try:
        rows = registry.transition(uow_id, UoWStatus.READY_FOR_EXECUTOR, UoWStatus.PRESCRIBING)
        if rows == 0:
            log.warning(
                "prescription-agent: prescribing→ready-for-executor transition race for %s — "
                "another process may have already advanced this UoW",
                uow_id,
            )
            return 0
        log.info(
            "prescription-agent: UoW %s transitioned to ready-for-executor (cycles=%d)",
            uow_id, new_cycles,
        )
    except Exception as exc:
        log.error(
            "prescription-agent: ready-for-executor transition failed for %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
