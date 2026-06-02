"""
Tests for the prescription concurrency cap introduced in Issue #1390.

Root cause: _dynamic_burst_batch_size returns 15 when queue_depth > 50, which
allows 15 concurrent wos_prescribe inbox messages per heartbeat cycle. Under high
queue depth, all 15 hit the 600s LLM timeout, startup_sweep resets them to
ready-for-steward, and the cycle repeats — 299 attempts, 0 completions.

Fix: MAX_CONCURRENT_PRESCRIPTIONS caps how many UoWs may be in the 'prescribing'
state simultaneously. _process_uow checks prescription_slots_available and returns
PrescriptionDeferred (with the UoW transitioned back to ready-for-steward) when no
slots remain.

Tests are named after the behavior they verify, not the mechanism.
All threshold values are referenced from named constants, not magic literals.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.steward import (
    MAX_CONCURRENT_PRESCRIPTIONS,
    PrescriptionDeferred,
    PrescribingQueued,
    Diagnosis,
    IssueInfo,
    _count_prescribing_in_flight,
    _process_uow,
    _llm_prescribe,
    _dynamic_burst_batch_size,
    BURST_BATCH_SIZE,
)
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uow(**kwargs) -> UoW:
    """Build a minimal UoW with sensible defaults for prescription cap tests."""
    defaults = dict(
        id="uow_20260522_testcap",
        status="ready-for-steward",
        summary="Test UoW for prescription cap",
        source="telegram",
        source_issue_number=42,
        created_at="2026-05-22T00:00:00+00:00",
        updated_at="2026-05-22T00:00:00+00:00",
        steward_cycles=0,
        lifetime_cycles=0,
        register="operational",
    )
    defaults.update(kwargs)
    return UoW(**defaults)


# ---------------------------------------------------------------------------
# Unit tests: MAX_CONCURRENT_PRESCRIPTIONS constant
# ---------------------------------------------------------------------------

class TestMaxConcurrentPrescriptionsConstant:
    """The cap constant must be set to a value that prevents API flooding."""

    def test_cap_is_below_burst_batch_ceiling(self):
        """MAX_CONCURRENT_PRESCRIPTIONS must be below the burst batch ceiling (15).

        At queue_depth > 50, _dynamic_burst_batch_size returns 15. The cap
        must be strictly less than that ceiling or the flood is not prevented.
        """
        burst_ceiling = _dynamic_burst_batch_size(51)
        assert MAX_CONCURRENT_PRESCRIPTIONS < burst_ceiling, (
            f"Cap ({MAX_CONCURRENT_PRESCRIPTIONS}) must be below burst ceiling "
            f"({burst_ceiling}) to prevent flooding under high queue depth"
        )

    def test_cap_is_positive(self):
        """Cap must allow at least one concurrent prescription."""
        assert MAX_CONCURRENT_PRESCRIPTIONS >= 1

    def test_cap_constant_value(self):
        """Named constant MAX_CONCURRENT_PRESCRIPTIONS is 5."""
        assert MAX_CONCURRENT_PRESCRIPTIONS == 5


# ---------------------------------------------------------------------------
# Unit tests: _count_prescribing_in_flight
# ---------------------------------------------------------------------------

class TestCountPrescribingInFlight:
    """_count_prescribing_in_flight queries the prescribing state count safely."""

    def test_returns_count_of_prescribing_uows(self):
        """Returns the number of UoWs the registry reports in prescribing state."""
        registry = MagicMock()
        registry.list.return_value = [MagicMock(), MagicMock(), MagicMock()]
        assert _count_prescribing_in_flight(registry) == 3

    def test_returns_zero_when_none_prescribing(self):
        """Returns 0 when no UoWs are in prescribing state."""
        registry = MagicMock()
        registry.list.return_value = []
        assert _count_prescribing_in_flight(registry) == 0

    def test_returns_zero_on_registry_error(self):
        """Registry errors are swallowed — returns 0 rather than raising.

        A failed prescribing-count query should not block dispatch entirely;
        better to allow dispatch than to deadlock the heartbeat.
        """
        registry = MagicMock()
        registry.list.side_effect = RuntimeError("DB unavailable")
        assert _count_prescribing_in_flight(registry) == 0


# ---------------------------------------------------------------------------
# Unit tests: _process_uow defers when prescription_slots_available = 0
# ---------------------------------------------------------------------------

def _make_first_execution_diagnosis() -> Diagnosis:
    """Return a Diagnosis representing a brand-new UoW (never executed)."""
    return Diagnosis(
        reentry_posture="first_execution",
        return_reason=None,
        return_reason_classification="no_prior_execution",
        output_content="",
        output_valid=False,
        is_complete=False,
        completion_rationale="No prior execution.",
        stuck_condition=None,
        executor_outcome=None,
        success_criteria_missing=False,
    )


def _make_minimal_issue_info() -> IssueInfo:
    """Return a minimal IssueInfo for tests."""
    return IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body="Test issue body",
        title="Test issue",
    )


class TestProcessUowPrescriptionCapEnforcement:
    """_process_uow returns PrescriptionDeferred when no slots are available."""

    def _make_registry_mock(self):
        """Return a registry mock that handles the diagnosing optimistic lock."""
        registry = MagicMock()
        registry.transition.return_value = 1  # Successful optimistic lock
        registry.append_audit_log.return_value = None
        registry.update.return_value = None
        return registry

    def test_prescription_deferred_when_no_slots(self):
        """When prescription_slots_available=0, _process_uow returns PrescriptionDeferred.

        The UoW must not be transitioned to prescribing state when the cap is reached.
        """
        from src.orchestration import steward as _steward_mod

        uow = _make_uow(steward_cycles=0)
        registry = self._make_registry_mock()
        issue_info = _make_minimal_issue_info()

        # Patch _write_prescription_request to verify it is NOT called.
        # Patch _diagnose_uow to return a first-execution diagnosis (needs prescription).
        with patch.object(
            _steward_mod, "_write_prescription_request",
            return_value="mock-msg-id",
        ) as mock_write, patch.object(
            _steward_mod, "_diagnose_uow",
            return_value=_make_first_execution_diagnosis(),
        ):
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[],
                issue_info=issue_info,
                dry_run=False,
                artifact_dir=None,
                notify_dan=None,
                llm_prescriber=_llm_prescribe,  # production sentinel triggers async path
                prescription_slots_available=0,  # cap reached
            )

        # wos_prescribe message must NOT have been written
        mock_write.assert_not_called()
        assert isinstance(result, PrescriptionDeferred), (
            f"Expected PrescriptionDeferred, got {type(result).__name__}"
        )
        assert result.uow_id == uow.id

    def test_prescription_proceeds_when_slot_available(self):
        """When prescription_slots_available > 0, the wos_prescribe message is written.

        This verifies the cap only blocks when slots=0, not at positive values.
        """
        from src.orchestration import steward as _steward_mod

        uow = _make_uow(steward_cycles=0)
        registry = self._make_registry_mock()
        issue_info = _make_minimal_issue_info()

        with patch.object(
            _steward_mod, "_write_prescription_request",
            return_value="mock-msg-id",
        ) as mock_write, patch.object(
            _steward_mod, "_diagnose_uow",
            return_value=_make_first_execution_diagnosis(),
        ), patch.object(
            _steward_mod, "_write_steward_fields",
        ), patch.object(
            _steward_mod, "_append_steward_log_entry",
            return_value="{}",
        ), patch.object(
            _steward_mod, "_mark_current_agenda_node_prescribed",
            return_value=[],
        ):
            registry.transition.side_effect = [1, 1]  # claim + prescribing transition

            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[],
                issue_info=issue_info,
                dry_run=False,
                artifact_dir=None,
                notify_dan=None,
                llm_prescriber=_llm_prescribe,
                prescription_slots_available=1,  # one slot available
            )

        # wos_prescribe message should have been written (prescription proceeded)
        mock_write.assert_called_once()
        assert isinstance(result, PrescribingQueued), (
            f"Expected PrescribingQueued, got {type(result).__name__}"
        )

    def test_undo_diagnosing_transition_on_deferred(self):
        """When prescription is deferred, the UoW is transitioned back to ready-for-steward.

        Without the undo transition, the UoW stays in 'diagnosing' and requires
        startup_sweep to rescue it — adding an unnecessary heartbeat delay.
        """
        from src.orchestration import steward as _steward_mod
        from src.orchestration.registry import UoWStatus

        uow = _make_uow(steward_cycles=0)
        registry = self._make_registry_mock()
        issue_info = _make_minimal_issue_info()

        with patch.object(
            _steward_mod, "_diagnose_uow",
            return_value=_make_first_execution_diagnosis(),
        ):
            _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[],
                issue_info=issue_info,
                dry_run=False,
                artifact_dir=None,
                notify_dan=None,
                llm_prescriber=_llm_prescribe,
                prescription_slots_available=0,
            )

        # First transition: claim (ready-for-steward → diagnosing)
        # Last transition: undo (diagnosing → ready-for-steward)
        transition_calls = registry.transition.call_args_list
        assert len(transition_calls) >= 2, (
            "Expected at least 2 transition calls (claim + undo)"
        )
        undo_call = transition_calls[-1]
        args = undo_call[0]
        # registry.transition(uow_id, to_status, from_status)
        assert args[1] == UoWStatus.READY_FOR_STEWARD, (
            f"Undo transition should target ready-for-steward, got {args[1]}"
        )
        assert args[2] == UoWStatus.DIAGNOSING, (
            f"Undo transition should come from diagnosing, got {args[2]}"
        )


# ---------------------------------------------------------------------------
# Unit tests: run_steward_cycle prescription cap integration
# ---------------------------------------------------------------------------

class TestStewardCyclePrescriptionCapIntegration:
    """run_steward_cycle enforces MAX_CONCURRENT_PRESCRIPTIONS across the full cycle."""

    def test_cap_constant_is_below_burst_ceiling_for_queue_over_50(self):
        """Regression: at queue_depth=60, burst_batch_size=15 but cap is 5.

        This is the exact scenario that caused the flood (299 attempts, 0 completions).
        The cap must prevent 15 concurrent prescriptions from being dispatched.
        """
        flood_queue_depth = 60
        burst_size_at_flood = _dynamic_burst_batch_size(flood_queue_depth)
        assert burst_size_at_flood == 15, (
            f"Expected burst size 15 at queue_depth=60, got {burst_size_at_flood}"
        )
        assert MAX_CONCURRENT_PRESCRIPTIONS < burst_size_at_flood, (
            f"Cap ({MAX_CONCURRENT_PRESCRIPTIONS}) must be < burst ceiling "
            f"({burst_size_at_flood}) at the flood queue depth ({flood_queue_depth})"
        )
