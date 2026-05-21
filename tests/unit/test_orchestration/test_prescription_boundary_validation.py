"""
Tests for boundary_present field in LLMPrescription — 2026-05-03 prescription audit.

Coverage:
- warn-on-missing-Boundary: _llm_prescribe logs warning and sets boundary_present=False
- no-warn-on-present-Boundary: no warning logged, boundary_present=True
- backwards-compat default: LLMPrescription(instructions=...) without boundary_present
  still constructs with boundary_present=False
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    _llm_prescribe,
    LLMPrescription,
)
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uow(uow_id: str = "uow_test_boundary") -> UoW:
    """Return a minimal UoW stub for _llm_prescribe."""
    now = "2026-05-19T00:00:00Z"
    return UoW(
        id=uow_id,
        status="diagnosing",
        summary="Implement feature X",
        source="github:issue/42",
        source_issue_number=42,
        created_at=now,
        updated_at=now,
        sweep_date="2026-05-19",
        success_criteria="Output file exists",
        steward_cycles=0,
        lifetime_cycles=0,
        execution_attempts=0,
        steward_log=None,
    )


def _make_artifact_text(instructions: str) -> str:
    """Wrap instructions in the front-matter format _parse_workflow_artifact expects."""
    return (
        "---\n"
        "executor_type: functional-engineer\n"
        "estimated_cycles: 1\n"
        "success_criteria_check: Tests pass\n"
        "---\n"
        f"\n{instructions}\n"
    )


def _mock_subprocess_success(stdout: str):
    """Return a (proc, None) pair that mimics a successful subprocess call."""
    proc = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return proc, None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBoundaryPresentField:
    """Unit tests for the boundary_present field on LLMPrescription."""

    def test_boundary_present_field_defaults_false(self) -> None:
        """Constructing LLMPrescription without boundary_present yields False (backwards compat)."""
        prescription = LLMPrescription(
            instructions="Do the thing.",
            success_criteria_check="Output exists",
            estimated_cycles=1,
        )
        assert prescription.boundary_present is False, (
            "boundary_present must default to False so existing test stubs "
            "that omit the field continue to work"
        )

    def test_boundary_present_true_when_set(self) -> None:
        """Explicit boundary_present=True is stored correctly."""
        prescription = LLMPrescription(
            instructions="Do the thing. Boundary: do not touch prod.",
            success_criteria_check="Output exists",
            estimated_cycles=1,
            boundary_present=True,
        )
        assert prescription.boundary_present is True


class TestLlmPrescribeWarnOnMissingBoundary:
    """Tests for _llm_prescribe warn-on-missing-Boundary behavior."""

    def test_llm_prescribe_warns_when_boundary_missing(self, caplog) -> None:
        """
        When the LLM returns instructions without a 'Boundary:' clause,
        _llm_prescribe must log a warning and return boundary_present=False.
        """
        instructions_without_boundary = (
            "Implement the feature as described in the issue.\n"
            "Run tests before committing.\n"
            "Open a PR when done."
        )
        artifact = _make_artifact_text(instructions_without_boundary)
        uow = _make_uow()

        with patch(
            "src.orchestration.steward.run_subprocess_with_error_capture",
            return_value=_mock_subprocess_success(artifact),
        ):
            import logging
            with caplog.at_level(logging.WARNING, logger="src.orchestration.steward"):
                result = _llm_prescribe(
                    uow=uow,
                    reentry_posture="solo",
                    completion_gap="",
                )

        assert result is not None, "_llm_prescribe should return a prescription, not None"
        assert result.boundary_present is False, (
            "boundary_present must be False when instructions lack 'Boundary:'"
        )
        warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "missing explicit Boundary clause" in msg for msg in warning_texts
        ), (
            f"Expected warning containing 'missing explicit Boundary clause'. "
            f"Warnings logged: {warning_texts}"
        )

    def test_llm_prescribe_no_warn_when_boundary_present(self, caplog) -> None:
        """
        When the LLM returns instructions that include 'Boundary:', no warning
        should be logged and boundary_present must be True.
        """
        instructions_with_boundary = (
            "Implement the feature as described in the issue.\n"
            "Boundary: do not modify files outside src/orchestration/.\n"
            "Run tests before committing.\n"
            "Open a PR when done."
        )
        artifact = _make_artifact_text(instructions_with_boundary)
        uow = _make_uow()

        with patch(
            "src.orchestration.steward.run_subprocess_with_error_capture",
            return_value=_mock_subprocess_success(artifact),
        ):
            import logging
            with caplog.at_level(logging.WARNING, logger="src.orchestration.steward"):
                result = _llm_prescribe(
                    uow=uow,
                    reentry_posture="solo",
                    completion_gap="",
                )

        assert result is not None, "_llm_prescribe should return a prescription, not None"
        assert result.boundary_present is True, (
            "boundary_present must be True when instructions include 'Boundary:'"
        )
        boundary_warnings = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "Boundary clause" in r.message
        ]
        assert len(boundary_warnings) == 0, (
            f"No Boundary-clause warning should be logged when Boundary: is present. "
            f"Got: {boundary_warnings}"
        )
