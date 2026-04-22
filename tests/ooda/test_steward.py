"""
tests/ooda/test_steward.py

Unit tests for the StewardHeartbeat class.

Coverage:
- test_diagnose_writes_vision_fields: diagnosis record contains vision_fields_cited
  from mocked get_vision_context()
- test_prescribe_writes_workflow_artifact: prescription writes a file at expected path
- test_audit_written_before_state_transition: audit entry file exists before registry
  status changes
- test_convergence_closes_uow: when convergence condition met, status transitions to done
- test_anticonvergence_surfaces_alert: when 3 cycles produce no artifact, admin alert triggered

All tests use pytest. External calls (MCP, registry, filesystem writes) are mocked.
"""

from __future__ import annotations

import json
import sys
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Ensure the src package is importable from the worktree
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.steward import (
    StewardHeartbeat,
    DiagnosisRecord,
    PrescriptionRecord,
    select_workflow,
    ReentryPosture,
    StuckCondition,
    UoWStatus,
    _get_vision_context_via_mcp,
    WORKFLOW_INVESTIGATION,
    WORKFLOW_EXECUTION_PASS,
    WORKFLOW_SYNTHESIS_PASS,
)
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_uow(
    uow_id: str = "uow_20260422_test01",
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    output_ref: str | None = None,
    success_criteria: str = "Output file exists and contains findings.",
    summary: str = "Test UoW summary.",
    close_reason: str | None = None,
    prescribed_skills: list | None = None,
) -> UoW:
    return UoW(
        id=uow_id,
        status=UoWStatus(status),
        summary=summary,
        source="github",
        source_issue_number=None,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        success_criteria=success_criteria,
        output_ref=output_ref,
        steward_cycles=steward_cycles,
        lifetime_cycles=steward_cycles,
        close_reason=close_reason,
        prescribed_skills=prescribed_skills or [],
    )


def _make_diagnosis(
    uow_id: str = "uow_20260422_test01",
    cycle: int = 0,
    reentry_posture: str = ReentryPosture.FIRST_EXECUTION,
    is_complete: bool = False,
    output_valid: bool = False,
    stuck_condition: str | None = None,
    vision_fields_cited: dict | None = None,
    vision_unavailable: bool = False,
) -> DiagnosisRecord:
    return DiagnosisRecord(
        uow_id=uow_id,
        cycle=cycle,
        reentry_posture=reentry_posture,
        return_reason=None,
        is_complete=is_complete,
        completion_rationale="",
        stuck_condition=stuck_condition,
        output_valid=output_valid,
        vision_fields_cited=vision_fields_cited or {},
        vision_unavailable=vision_unavailable,
    )


# ---------------------------------------------------------------------------
# test_diagnose_writes_vision_fields
# ---------------------------------------------------------------------------

class TestDiagnoseWritesVisionFields:
    """diagnose() must include vision_fields_cited from get_vision_context."""

    def test_diagnose_writes_vision_fields(self, tmp_path):
        """DiagnosisRecord contains vision_fields_cited when MCP returns data."""
        mock_vision_data = {
            "active_project": "WOS steward implementation",
            "current_focus": "Building heartbeat loop",
        }

        uow = _make_uow()

        # Patch the vision context fetch function
        with patch(
            "src.orchestration.steward._get_vision_context_via_mcp",
            return_value=mock_vision_data,
        ):
            # Patch _diagnose_uow and _fetch_audit_entries to avoid DB
            with patch("src.orchestration.steward._diagnose_uow") as mock_diagnose:
                from src.orchestration.steward import Diagnosis
                mock_diagnose.return_value = Diagnosis(
                    reentry_posture=ReentryPosture.FIRST_EXECUTION,
                    return_reason=None,
                    return_reason_classification="normal",
                    output_content="",
                    output_valid=False,
                    is_complete=False,
                    completion_rationale="",
                    stuck_condition=None,
                    executor_outcome=None,
                    success_criteria_missing=False,
                )
                with patch("src.orchestration.steward._fetch_audit_entries", return_value=[]):
                    heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path)
                    diagnosis = heartbeat.diagnose(uow)

        assert "active_project" in diagnosis.vision_fields_cited
        assert "current_focus" in diagnosis.vision_fields_cited
        assert diagnosis.vision_fields_cited["active_project"] == "WOS steward implementation"
        assert diagnosis.vision_fields_cited["current_focus"] == "Building heartbeat loop"
        assert diagnosis.vision_unavailable is False

    def test_diagnose_marks_vision_unavailable_when_mcp_fails(self, tmp_path):
        """When get_vision_context returns empty, vision_unavailable=True is set."""
        uow = _make_uow()

        with patch(
            "src.orchestration.steward._get_vision_context_via_mcp",
            return_value={},
        ):
            with patch("src.orchestration.steward._diagnose_uow") as mock_diagnose:
                from src.orchestration.steward import Diagnosis
                mock_diagnose.return_value = Diagnosis(
                    reentry_posture=ReentryPosture.FIRST_EXECUTION,
                    return_reason=None,
                    return_reason_classification="normal",
                    output_content="",
                    output_valid=False,
                    is_complete=False,
                    completion_rationale="",
                    stuck_condition=None,
                    executor_outcome=None,
                    success_criteria_missing=False,
                )
                with patch("src.orchestration.steward._fetch_audit_entries", return_value=[]):
                    heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path)
                    diagnosis = heartbeat.diagnose(uow)

        assert diagnosis.vision_unavailable is True
        assert diagnosis.vision_fields_cited == {}


# ---------------------------------------------------------------------------
# test_prescribe_writes_workflow_artifact
# ---------------------------------------------------------------------------

class TestPrescribeWritesWorkflowArtifact:
    """prescribe() must write a file at the expected artifact path."""

    def test_prescribe_writes_workflow_artifact(self, tmp_path):
        """Prescription writes a workflow artifact file at the expected path."""
        uow = _make_uow()
        diagnosis = _make_diagnosis(
            vision_fields_cited={"active_project": "proj", "current_focus": "focus"},
        )

        artifact_path = tmp_path / f"{uow.id}.md"

        with patch(
            "src.orchestration.steward._write_workflow_artifact",
            return_value=str(artifact_path),
        ) as mock_write:
            # Write a dummy artifact file so path check passes
            artifact_path.write_text("# test artifact", encoding="utf-8")

            heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path, dry_run=False)
            result = heartbeat.prescribe(uow, diagnosis)

        assert mock_write.called
        assert result.workflow_artifact_path == str(artifact_path)
        assert result.workflow_selected in (
            WORKFLOW_INVESTIGATION,
            WORKFLOW_EXECUTION_PASS,
            WORKFLOW_SYNTHESIS_PASS,
            "single_assessment",
            "design_review",
            "diverge_converge_1x",
            "diverge_converge_2x",
            "multi_perspective_fanout",
            "spec_breakdown",
        )
        assert result.rationale  # non-empty rationale

    def test_prescribe_dry_run_skips_artifact_write(self, tmp_path):
        """In dry_run mode, no artifact file is written."""
        uow = _make_uow()
        diagnosis = _make_diagnosis()

        with patch("src.orchestration.steward._write_workflow_artifact") as mock_write:
            heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path, dry_run=True)
            result = heartbeat.prescribe(uow, diagnosis)

        mock_write.assert_not_called()
        assert result.workflow_artifact_path is None


# ---------------------------------------------------------------------------
# test_audit_written_before_state_transition
# ---------------------------------------------------------------------------

class TestAuditWrittenBeforeStateTransition:
    """Audit entry file must exist before registry status changes."""

    def test_audit_written_before_state_transition(self, tmp_path):
        """write_audit_entry writes file; state transition only called after."""
        uow = _make_uow()
        diagnosis = _make_diagnosis()
        prescription = PrescriptionRecord(
            uow_id=uow.id,
            cycle=0,
            workflow_selected=WORKFLOW_INVESTIGATION,
            rationale="First cycle investigation.",
            workflow_artifact_path=str(tmp_path / "artifact.md"),
        )

        mock_registry = MagicMock()
        transition_called_after_audit = []

        def mock_transition(uow_id, new_status, from_status):
            # Check that audit file exists when transition is called
            audit_file = tmp_path / uow_id / "cycle_000.json"
            transition_called_after_audit.append(audit_file.exists())
            return 1

        mock_registry.transition.side_effect = mock_transition

        heartbeat = StewardHeartbeat(registry=mock_registry, audit_dir=tmp_path, dry_run=False)

        # Write audit entry (this is the action under test)
        heartbeat.write_audit_entry(uow, diagnosis, prescription, outcome="prescribed")

        # Verify audit file was written
        audit_file = tmp_path / uow.id / "cycle_000.json"
        assert audit_file.exists(), "Audit file must exist after write_audit_entry()"

        # Trigger close (which calls transition) to verify ordering
        heartbeat.close(uow, diagnosis, "test close")
        # transition was called at least once
        assert mock_registry.transition.called

        # All transitions happened after audit was written
        # (transition_called_after_audit was populated during mock_transition)
        if transition_called_after_audit:
            assert all(transition_called_after_audit), (
                "State transition was called before audit file existed"
            )

    def test_audit_entry_content_is_valid_json(self, tmp_path):
        """Audit entry file contains valid JSON with required fields."""
        uow = _make_uow()
        diagnosis = _make_diagnosis(
            vision_fields_cited={"active_project": "test_proj"},
        )
        prescription = PrescriptionRecord(
            uow_id=uow.id,
            cycle=0,
            workflow_selected=WORKFLOW_INVESTIGATION,
            rationale="Investigation rationale.",
            workflow_artifact_path=None,
        )

        heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path, dry_run=False)
        heartbeat.write_audit_entry(uow, diagnosis, prescription, outcome="prescribed")

        audit_file = tmp_path / uow.id / "cycle_000.json"
        content = json.loads(audit_file.read_text(encoding="utf-8"))

        assert content["uow_id"] == uow.id
        assert content["cycle"] == 0
        assert "diagnosis" in content
        assert "prescription" in content
        assert "vision_fields_cited" in content["diagnosis"]
        assert content["diagnosis"]["vision_fields_cited"]["active_project"] == "test_proj"
        assert content["outcome"] == "prescribed"


# ---------------------------------------------------------------------------
# test_convergence_closes_uow
# ---------------------------------------------------------------------------

class TestConvergenceClosesUoW:
    """When convergence condition met, status transitions to done."""

    def test_convergence_closes_uow(self, tmp_path):
        """check_convergence returns True when complete + output valid."""
        uow = _make_uow(output_ref=str(tmp_path / "output.md"))
        # Create a non-empty output file
        (tmp_path / "output.md").write_text("# findings\nsome content", encoding="utf-8")

        diagnosis = _make_diagnosis(
            is_complete=True,
            output_valid=True,
        )

        heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path)
        converged, reason = heartbeat.check_convergence(uow, diagnosis)

        assert converged is True
        assert "satisfied" in reason.lower() or reason  # some reason provided

    def test_convergence_close_transitions_status(self, tmp_path):
        """close() transitions registry status to done."""
        uow = _make_uow()
        diagnosis = _make_diagnosis(is_complete=True, output_valid=True)

        mock_registry = MagicMock()
        mock_registry.transition.return_value = 1

        heartbeat = StewardHeartbeat(
            registry=mock_registry,
            audit_dir=tmp_path,
            dry_run=False,
        )
        heartbeat.close(uow, diagnosis, "Convergence condition met: output valid and complete.")

        # Should have called transition to done
        assert mock_registry.transition.called
        calls = mock_registry.transition.call_args_list
        # At least one call should be transitioning to done
        done_calls = [c for c in calls if "done" in str(c)]
        assert done_calls, f"Expected transition to 'done', got: {calls}"

    def test_no_convergence_when_incomplete(self, tmp_path):
        """check_convergence returns False when not complete."""
        uow = _make_uow()
        diagnosis = _make_diagnosis(is_complete=False, output_valid=False)

        heartbeat = StewardHeartbeat(registry=MagicMock(), audit_dir=tmp_path)
        converged, reason = heartbeat.check_convergence(uow, diagnosis)

        assert converged is False


# ---------------------------------------------------------------------------
# test_anticonvergence_surfaces_alert
# ---------------------------------------------------------------------------

class TestAnticonvergenceSurfacesAlert:
    """When 3 cycles produce no artifact, an admin alert is triggered."""

    def test_anticonvergence_surfaces_alert_after_3_cycles(self, tmp_path):
        """check_convergence surfaces alert when steward_cycles >= 3 and no output."""
        uow = _make_uow(steward_cycles=3)
        diagnosis = _make_diagnosis(
            cycle=3,
            is_complete=False,
            output_valid=False,
        )

        alert_calls = []

        def mock_notify(uow_id: str, message: str) -> None:
            alert_calls.append({"uow_id": uow_id, "message": message})

        heartbeat = StewardHeartbeat(
            registry=MagicMock(),
            audit_dir=tmp_path,
            notify_admin=mock_notify,
        )
        converged, _ = heartbeat.check_convergence(uow, diagnosis)

        assert converged is False
        assert len(alert_calls) == 1, f"Expected 1 alert, got {len(alert_calls)}"
        assert alert_calls[0]["uow_id"] == uow.id
        assert "3" in alert_calls[0]["message"] or "cycle" in alert_calls[0]["message"].lower()

    def test_no_alert_under_threshold(self, tmp_path):
        """No alert when steward_cycles < 3."""
        uow = _make_uow(steward_cycles=2)
        diagnosis = _make_diagnosis(cycle=2, is_complete=False, output_valid=False)

        alert_calls = []

        def mock_notify(uow_id: str, message: str) -> None:
            alert_calls.append({"uow_id": uow_id, "message": message})

        heartbeat = StewardHeartbeat(
            registry=MagicMock(),
            audit_dir=tmp_path,
            notify_admin=mock_notify,
        )
        heartbeat.check_convergence(uow, diagnosis)

        assert len(alert_calls) == 0, "No alert expected under 3-cycle threshold"

    def test_alert_at_exactly_3_cycles(self, tmp_path):
        """Alert fires at exactly 3 cycles with no output."""
        uow = _make_uow(steward_cycles=3)
        diagnosis = _make_diagnosis(cycle=3, is_complete=False, output_valid=False)

        alert_fired = []
        heartbeat = StewardHeartbeat(
            registry=MagicMock(),
            audit_dir=tmp_path,
            notify_admin=lambda uid, msg: alert_fired.append(msg),
        )
        heartbeat.check_convergence(uow, diagnosis)
        assert len(alert_fired) == 1


# ---------------------------------------------------------------------------
# select_workflow tests
# ---------------------------------------------------------------------------

class TestSelectWorkflow:
    """Tests for the workflow primitive selection function."""

    def test_investigation_on_first_cycle(self):
        """First cycle with no output → investigation."""
        diagnosis = _make_diagnosis(
            cycle=0,
            reentry_posture=ReentryPosture.FIRST_EXECUTION,
            output_valid=False,
        )
        workflow, rationale = select_workflow(diagnosis)
        assert workflow == WORKFLOW_INVESTIGATION
        assert rationale

    def test_synthesis_on_complete_with_output(self):
        """Complete + output valid → synthesis_pass."""
        diagnosis = _make_diagnosis(
            cycle=1,
            reentry_posture=ReentryPosture.EXECUTION_COMPLETE,
            is_complete=True,
            output_valid=True,
        )
        workflow, rationale = select_workflow(diagnosis)
        assert workflow == WORKFLOW_SYNTHESIS_PASS

    def test_execution_pass_on_valid_output(self):
        """Prior output valid + non-zero cycle → execution_pass."""
        diagnosis = _make_diagnosis(
            cycle=2,
            reentry_posture=ReentryPosture.EXECUTION_COMPLETE,
            is_complete=False,
            output_valid=True,
        )
        workflow, rationale = select_workflow(diagnosis)
        assert workflow == WORKFLOW_EXECUTION_PASS
