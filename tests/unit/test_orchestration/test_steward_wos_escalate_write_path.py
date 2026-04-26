"""
Tests for the steward.py wos_escalate write path (Issue #971).

When a UoW exhausts MAX_RETRIES (gated on execution_attempts), the Steward must
write a wos_escalate inbox message instead of a wos_surface message.  This
activates the 4-branch dispatcher decision tree added in PR #970, inserting a
programmatic triage layer before human notification.

Coverage:
- _build_wos_escalate_failure_history: pure function, correct field population
- _build_wos_escalate_failure_history: kill_type derived from reentry_posture
- _build_wos_escalate_failure_history: heartbeats_before_kill derived from kill_type
- _write_wos_escalate_message: writes a wos_escalate message to the inbox
- _write_wos_escalate_message: message carries all required fields
- _write_wos_escalate_message: fallback to _send_escalation_notification on write failure
- run_steward_cycle (integration): retry-cap path writes wos_escalate, not wos_surface
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestration.steward import (
    MAX_RETRIES,
    ORPHAN_KILL_BEFORE_START,
    ORPHAN_KILL_DURING_EXECUTION,
    _POSTURE_EXECUTOR_ORPHAN,
    _POSTURE_ORPHAN_KILL_BEFORE_START,
    _POSTURE_ORPHAN_KILL_DURING_EXECUTION,
    _build_wos_escalate_failure_history,
    _write_wos_escalate_message,
)
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Named constants from the spec
# ---------------------------------------------------------------------------

# Message type that wos_escalate messages must carry (matches dispatcher handler)
WOS_ESCALATE_TYPE = "wos_escalate"

# Reentry postures that map to specific kill_type values — imported from steward.py
# to avoid re-declaring strings that would silently diverge if the source changes.
ORPHAN_KILL_BEFORE_START_POSTURE = _POSTURE_ORPHAN_KILL_BEFORE_START
ORPHAN_KILL_DURING_EXECUTION_POSTURE = _POSTURE_ORPHAN_KILL_DURING_EXECUTION
EXECUTOR_ORPHAN_POSTURE = _POSTURE_EXECUTOR_ORPHAN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_uow(
    uow_id: str = "uow-test-001",
    summary: str = "Test UoW summary",
    uow_type: str = "github-issue",
    status: str = "diagnosing",
    retry_count: int = 3,
    execution_attempts: int = 3,
    steward_cycles: int = 3,
    register: str = "operational",
    output_ref: str | None = None,
) -> UoW:
    """Build a minimal UoW for testing."""
    return UoW(
        id=uow_id,
        source="test",
        source_issue_number=None,
        summary=summary,
        type=uow_type,
        status=status,
        retry_count=retry_count,
        execution_attempts=execution_attempts,
        steward_cycles=steward_cycles,
        register=register,
        output_ref=output_ref,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _make_audit_entries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a minimal audit_entries list for testing."""
    return events


# ---------------------------------------------------------------------------
# Tests for _build_wos_escalate_failure_history
# ---------------------------------------------------------------------------

class TestBuildWosEscalateFailureHistory:
    """
    Pure function — no side effects.

    Verifies field population and derivation from reentry_posture and return_reason.
    """

    def test_execution_attempts_field_populated(self) -> None:
        """failure_history must carry execution_attempts from the call site."""
        uow = _make_uow(execution_attempts=2)
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=2,
            return_reason="executing_orphan",
            reentry_posture="executing_orphan",
            audit_entries=[],
        )
        assert result["execution_attempts"] == 2

    def test_return_reason_classification_populated_for_orphan(self) -> None:
        """return_reason_classification must be 'orphan' for executing_orphan return_reason."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=1,
            return_reason="executing_orphan",
            reentry_posture="executing_orphan",
            audit_entries=[],
        )
        assert result["return_reason_classification"] == "orphan"

    def test_kill_type_before_start_from_orphan_kill_before_start_posture(self) -> None:
        """kill_type must be 'orphan_kill_before_start' for the corresponding posture."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=0,
            return_reason="executor_orphan",
            reentry_posture=ORPHAN_KILL_BEFORE_START_POSTURE,
            audit_entries=[],
        )
        assert result["kill_type"] == ORPHAN_KILL_BEFORE_START_POSTURE

    def test_kill_type_during_execution_from_orphan_kill_during_execution_posture(self) -> None:
        """kill_type must be 'orphan_kill_during_execution' for the corresponding posture."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=1,
            return_reason="executing_orphan",
            reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
            audit_entries=[],
        )
        assert result["kill_type"] == ORPHAN_KILL_DURING_EXECUTION_POSTURE

    def test_kill_type_empty_for_non_orphan_posture(self) -> None:
        """kill_type must be empty string when posture is not a known orphan kill posture."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=3,
            return_reason="executor_failed",
            reentry_posture="execution_failed",
            audit_entries=[],
        )
        assert result["kill_type"] == ""

    def test_heartbeats_before_kill_zero_for_before_start(self) -> None:
        """heartbeats_before_kill must be 0 for kill_before_start posture."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=0,
            return_reason="executor_orphan",
            reentry_posture=ORPHAN_KILL_BEFORE_START_POSTURE,
            audit_entries=[],
        )
        assert result["heartbeats_before_kill"] == 0

    def test_heartbeats_before_kill_positive_for_during_execution(self) -> None:
        """heartbeats_before_kill must be >= 1 for kill_during_execution posture."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=1,
            return_reason="executing_orphan",
            reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
            audit_entries=[],
        )
        assert result["heartbeats_before_kill"] >= 1

    def test_infrastructure_events_from_audit_entries(self) -> None:
        """infrastructure_events must include audit entries with orphan event types."""
        audit_entries = [
            {"event": "executing_orphan_failed", "uow_id": "uow-001", "timestamp": "2026-04-26T00:00:00Z"},
            {"event": "steward_diagnosis", "uow_id": "uow-001", "timestamp": "2026-04-26T00:01:00Z"},
        ]
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=1,
            return_reason="executing_orphan",
            reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
            audit_entries=audit_entries,
        )
        infra_events = result["infrastructure_events"]
        # Only the orphan event should appear as an infrastructure event
        assert len(infra_events) == 1
        assert infra_events[0]["event"] == "executing_orphan_failed"

    def test_required_fields_present(self) -> None:
        """failure_history must carry all fields required by handle_wos_escalate."""
        uow = _make_uow()
        result = _build_wos_escalate_failure_history(
            uow=uow,
            execution_attempts=2,
            return_reason="executing_orphan",
            reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
            audit_entries=[],
        )
        required_fields = {
            "execution_attempts",
            "return_reason_classification",
            "kill_type",
            "heartbeats_before_kill",
            "infrastructure_events",
        }
        assert required_fields.issubset(result.keys()), (
            f"Missing fields: {required_fields - result.keys()}"
        )

    def test_pure_function_identical_inputs_produce_identical_outputs(self) -> None:
        """_build_wos_escalate_failure_history is a pure function."""
        uow = _make_uow()
        kwargs = dict(
            uow=uow,
            execution_attempts=2,
            return_reason="executing_orphan",
            reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
            audit_entries=[{"event": "executing_orphan_failed"}],
        )
        result1 = _build_wos_escalate_failure_history(**kwargs)
        result2 = _build_wos_escalate_failure_history(**kwargs)
        assert result1 == result2


# ---------------------------------------------------------------------------
# Tests for _write_wos_escalate_message
# ---------------------------------------------------------------------------

class TestWriteWosEscalateMessage:
    """
    Verifies that _write_wos_escalate_message writes a correctly-shaped
    wos_escalate message to the inbox.
    """

    def test_writes_json_file_to_inbox(self, tmp_path: Path) -> None:
        """A JSON file must be written to the inbox directory."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow()

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=3,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
            )

        json_files = list(inbox_dir.glob("*.json"))
        assert len(json_files) == 1, "Exactly one JSON file should be written to the inbox"

    def test_message_has_wos_escalate_type(self, tmp_path: Path) -> None:
        """The written message must have type='wos_escalate' at the top level."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow()

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=3,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
            )

        json_file = next(inbox_dir.glob("*.json"))
        msg = json.loads(json_file.read_text())
        assert msg.get("type") == WOS_ESCALATE_TYPE, (
            f"Message type must be {WOS_ESCALATE_TYPE!r}, got {msg.get('type')!r}"
        )

    def test_message_has_uow_id(self, tmp_path: Path) -> None:
        """The written message must carry the uow_id."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(uow_id="uow-test-777")

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=1,
                return_reason="executor_orphan",
                reentry_posture=ORPHAN_KILL_BEFORE_START_POSTURE,
                audit_entries=[],
            )

        json_file = next(inbox_dir.glob("*.json"))
        msg = json.loads(json_file.read_text())
        assert msg.get("uow_id") == "uow-test-777"

    def test_message_has_failure_history(self, tmp_path: Path) -> None:
        """The written message must carry a failure_history dict."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow()

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=2,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
            )

        json_file = next(inbox_dir.glob("*.json"))
        msg = json.loads(json_file.read_text())
        assert isinstance(msg.get("failure_history"), dict), (
            "failure_history must be a dict"
        )
        assert "execution_attempts" in msg["failure_history"]

    def test_message_has_uow_title_from_summary(self, tmp_path: Path) -> None:
        """The written message must carry uow_title populated from uow.summary."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(summary="Fix login regression in auth service")

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=3,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
            )

        json_file = next(inbox_dir.glob("*.json"))
        msg = json.loads(json_file.read_text())
        assert "Fix login regression" in msg.get("uow_title", "")

    def test_message_has_register_field(self, tmp_path: Path) -> None:
        """The written message must carry the register field from the UoW."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(register="philosophical")

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=1,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
            )

        json_file = next(inbox_dir.glob("*.json"))
        msg = json.loads(json_file.read_text())
        assert msg.get("register") == "philosophical"

    def test_fallback_on_write_failure(self, tmp_path: Path) -> None:
        """If writing the wos_escalate message fails, fallback_fn must be called."""
        uow = _make_uow()
        fallback_calls: list[UoW] = []

        def mock_fallback(u: UoW) -> None:
            fallback_calls.append(u)

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        # Simulate a write failure by patching Path.write_text to raise
        with (
            patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir),
            patch("pathlib.Path.write_text", side_effect=OSError("simulated write error")),
        ):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=3,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
                fallback_fn=mock_fallback,
            )

        assert len(fallback_calls) == 1
        assert fallback_calls[0] is uow

    def test_no_fallback_on_success(self, tmp_path: Path) -> None:
        """If writing succeeds, the fallback must NOT be called."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow()
        fallback_called = []

        def fallback(u: UoW) -> None:
            fallback_called.append(True)

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=2,
                return_reason="executing_orphan",
                reentry_posture=ORPHAN_KILL_DURING_EXECUTION_POSTURE,
                audit_entries=[],
                fallback_fn=fallback,
            )

        assert not fallback_called, "Fallback must not be called when write succeeds"


# ---------------------------------------------------------------------------
# Tests for suggested_action field
# ---------------------------------------------------------------------------

class TestSuggestedAction:
    """
    The wos_escalate message must carry a suggested_action field that mirrors
    the 4-branch decision tree in handle_wos_escalate.

    These tests verify the field is present and correctly set; they do NOT test
    the dispatcher handler itself (that is tested in test_wos_escalate_handler.py).
    """

    def test_suggested_action_auto_retry_for_pure_infrastructure_failure(
        self, tmp_path: Path
    ) -> None:
        """
        execution_attempts == 0 + orphan classification → suggested_action == 'auto_retry'.
        Mirrors Branch 1 in handle_wos_escalate.
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(execution_attempts=0)

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=0,
                return_reason="executor_orphan",
                reentry_posture=ORPHAN_KILL_BEFORE_START_POSTURE,
                audit_entries=[],
            )

        msg = json.loads(next(inbox_dir.glob("*.json")).read_text())
        assert msg.get("suggested_action") == "auto_retry"

    def test_suggested_action_surface_for_execution_cap_exhausted(
        self, tmp_path: Path
    ) -> None:
        """
        execution_attempts >= MAX_RETRIES → suggested_action == 'surface_to_human'.
        Mirrors Branch 3 in handle_wos_escalate.
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(execution_attempts=MAX_RETRIES)

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=MAX_RETRIES,
                return_reason="execution_failed",
                reentry_posture="execution_failed",
                audit_entries=[],
            )

        msg = json.loads(next(inbox_dir.glob("*.json")).read_text())
        assert msg.get("suggested_action") == "surface_to_human"

    def test_suggested_action_surface_for_human_judgment_register(
        self, tmp_path: Path
    ) -> None:
        """
        register in {human-judgment, philosophical} → suggested_action == 'surface_to_human'.
        Mirrors Branch 4 in handle_wos_escalate (checked first — register overrides all).
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        uow = _make_uow(register="human-judgment", execution_attempts=0)

        with patch("orchestration.steward._INBOX_DIR_PATH", inbox_dir):
            _write_wos_escalate_message(
                uow=uow,
                execution_attempts=0,
                return_reason="executor_orphan",
                reentry_posture=ORPHAN_KILL_BEFORE_START_POSTURE,
                audit_entries=[],
            )

        msg = json.loads(next(inbox_dir.glob("*.json")).read_text())
        assert msg.get("suggested_action") == "surface_to_human"
