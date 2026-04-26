"""
tests/ooda/test_retry_accounting_discriminator.py

Unit tests for the WOS retry accounting discriminator (issue #962).

The core behavior under test:
- MAX_RETRIES gates on execution_attempts (confirmed dispatches), NOT steward_cycles
- Orphan returns (executor_orphan, executing_orphan, diagnosing_orphan) must NOT
  consume execution_attempts budget — they are infrastructure events, not execution outcomes
- Normal execution failures (execution_complete with failed outcome, execution_failed,
  partial, blocked) DO consume execution_attempts budget
- retry_count (diagnostic counter) continues to increment on every re-entry for
  backward-compat visibility in notifications
- Three orphan events must NOT exhaust the retry cap and must NOT escalate to
  needs-human-review

Named constants from the spec (issue #962):
    MAX_RETRIES = 3                     — cap applied to execution_attempts
    ORPHAN_REASONS = {executor_orphan, executing_orphan, diagnosing_orphan}
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    MAX_RETRIES,
    ORPHAN_REASONS,
    _is_infrastructure_event,
    _process_uow,
    _send_escalation_notification,
)
from src.orchestration.registry import Registry, UoW, UoWStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_startup_sweep_audit_entry(classification: str) -> dict:
    """
    Build an audit entry dict in the format expected by _most_recent_return_reason.

    _most_recent_return_reason reads entry["note"] as JSON to extract classification
    for startup_sweep events. The flat dict format {classification: ...} doesn't work;
    the note must be a JSON string.
    """
    return {
        "event": "startup_sweep",
        "note": json.dumps({"classification": classification}),
        "from_status": "active",
        "to_status": "ready-for-steward",
    }


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _make_uow_row(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "diagnosing",
    steward_cycles: int = 1,
    lifetime_cycles: int = 1,
    retry_count: int = 0,
    execution_attempts: int = 0,
    output_ref: str | None = None,
    audit_log_entries: list[dict] | None = None,
    summary: str = "Test UoW",
    success_criteria: str = "Output exists",
    register: str = "operational",
) -> str:
    """Insert a UoW row and optional audit entries. Returns the uow_id."""
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles, lifetime_cycles,
             retry_count, execution_attempts, success_criteria, register, route_evidence,
             trigger, steward_agenda, steward_log)
        VALUES (?, 'executable', 'github:issue/99', 99, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}',
                NULL, NULL)
        """,
        (uow_id, status, now, now, summary, output_ref, steward_cycles,
         lifetime_cycles, retry_count, execution_attempts, success_criteria, register),
    )
    if audit_log_entries:
        for entry in audit_log_entries:
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), uow_id,
                 entry.get("event", "unknown"),
                 entry.get("from_status"),
                 entry.get("to_status"),
                 entry.get("agent"),
                 json.dumps(entry)),
            )
    conn.commit()
    return uow_id


def _get_uow_row(conn: sqlite3.Connection, uow_id: str) -> dict:
    row = conn.execute("SELECT * FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
    return dict(row) if row else {}


def _fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
    from src.orchestration.steward import LLMPrescription
    return LLMPrescription(
        instructions="do something",
        success_criteria_check="output exists",
        estimated_cycles=1,
    )


def _make_issue_info():
    from src.orchestration.steward import IssueInfo
    return IssueInfo(status_code=200, title="Test", body="", labels=[], state="open")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Test: ORPHAN_REASONS constant
# ---------------------------------------------------------------------------

class TestOrphanReasonsConstant:
    """ORPHAN_REASONS must enumerate all infrastructure-event return reasons."""

    def test_orphan_reasons_includes_executor_orphan(self):
        """executor_orphan = session killed before dispatch, must not consume retry budget."""
        assert "executor_orphan" in ORPHAN_REASONS

    def test_orphan_reasons_includes_executing_orphan(self):
        """executing_orphan = subagent dispatched but write_result never received."""
        assert "executing_orphan" in ORPHAN_REASONS

    def test_orphan_reasons_includes_diagnosing_orphan(self):
        """diagnosing_orphan = startup sweep classified as orphan during diagnosis."""
        assert "diagnosing_orphan" in ORPHAN_REASONS

    def test_orphan_reasons_does_not_include_execution_complete(self):
        """execution_complete is a normal re-entry — must consume execution budget."""
        assert "execution_complete" not in ORPHAN_REASONS

    def test_orphan_reasons_does_not_include_execution_failed(self):
        """execution_failed is a genuine execution outcome — must consume execution budget."""
        assert "execution_failed" not in ORPHAN_REASONS


# ---------------------------------------------------------------------------
# Test: _is_infrastructure_event pure function
# ---------------------------------------------------------------------------

class TestIsInfrastructureEvent:
    """_is_infrastructure_event must classify return_reason correctly."""

    def test_executor_orphan_is_infrastructure(self):
        assert _is_infrastructure_event("executor_orphan") is True

    def test_executing_orphan_is_infrastructure(self):
        assert _is_infrastructure_event("executing_orphan") is True

    def test_diagnosing_orphan_is_infrastructure(self):
        assert _is_infrastructure_event("diagnosing_orphan") is True

    def test_execution_complete_is_not_infrastructure(self):
        assert _is_infrastructure_event("execution_complete") is False

    def test_execution_failed_is_not_infrastructure(self):
        assert _is_infrastructure_event("execution_failed") is False

    def test_none_is_not_infrastructure(self):
        """None return_reason = first execution, not an infrastructure event."""
        assert _is_infrastructure_event(None) is False

    def test_crashed_no_output_is_not_infrastructure(self):
        """crashed_no_output is an execution error, not an infrastructure kill."""
        assert _is_infrastructure_event("crashed_no_output") is False


# ---------------------------------------------------------------------------
# Test: orphan returns do NOT consume execution_attempts budget
# ---------------------------------------------------------------------------

class TestOrphanDoesNotConsumeRetryBudget:
    """
    Three consecutive orphan returns must NOT exhaust MAX_RETRIES and must NOT
    escalate the UoW to needs-human-review.

    This is the primary regression test for the 2026-04-26 failure cohort: 19 UoWs
    with lifetime_cycles=0 were escalated after 3 orphan events that consumed
    retry_count but never represented actual execution attempts.
    """

    def test_single_orphan_does_not_increment_execution_attempts(
        self, db_path, registry, tmp_path
    ):
        """
        When return_reason is executor_orphan, execution_attempts must NOT increment.
        """
        conn = _open_db(db_path)
        orphan_entry = _make_startup_sweep_audit_entry("executor_orphan")
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            lifetime_cycles=1,
            retry_count=0,
            execution_attempts=0,
            audit_log_entries=[orphan_entry],
        )
        conn.close()

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.execution_attempts == 0

        with patch("src.orchestration.steward._send_escalation_notification"):
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[orphan_entry],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["execution_attempts"] == 0, (
            f"execution_attempts must remain 0 after an orphan return, got {row['execution_attempts']}"
        )

    def test_three_orphan_returns_do_not_exhaust_retry_cap(
        self, db_path, registry, tmp_path
    ):
        """
        Three orphan returns must NOT cause escalation to needs-human-review.
        This replicates the 2026-04-26 failure: 19 UoWs hit needs-human-review
        after 3 orphan events with zero actual execution attempts.
        """
        conn = _open_db(db_path)
        # Simulate 3 prior orphan events: steward_cycles=3, but execution_attempts=0
        orphan_entry = _make_startup_sweep_audit_entry("executor_orphan")
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=3,
            lifetime_cycles=3,
            retry_count=3,           # retry_count consumed by orphan events (old bug would escalate)
            execution_attempts=0,    # but no actual execution occurred
            audit_log_entries=[orphan_entry],
        )
        conn.close()

        uow = registry.get(uow_id)
        assert uow.retry_count == 3
        assert uow.execution_attempts == 0

        escalation_calls = []
        prescribe_calls = []

        def capture_escalation(uow_arg):
            escalation_calls.append(True)

        def capture_prescribe(uow_arg, reentry_posture, completion_gap, issue_body=""):
            prescribe_calls.append(True)
            return _fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body)

        with patch("src.orchestration.steward._send_escalation_notification", side_effect=capture_escalation):
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[orphan_entry],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=capture_prescribe,
            )

        # Must NOT have escalated — execution_attempts=0, below MAX_RETRIES
        assert not escalation_calls, (
            "UoW must NOT be escalated after 3 orphan events with 0 execution_attempts. "
            f"escalation_calls={escalation_calls}"
        )
        # Must have re-dispatched normally
        assert prescribe_calls, "UoW must have been re-dispatched (prescribed) after orphan recovery"

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["status"] != "needs-human-review", (
            f"UoW must not be in needs-human-review after 3 orphan events with 0 execution_attempts. "
            f"status={row['status']}"
        )
        assert row["execution_attempts"] == 0, (
            f"execution_attempts must remain 0 after orphan returns, got {row['execution_attempts']}"
        )

    def test_executing_orphan_does_not_increment_execution_attempts(
        self, db_path, registry, tmp_path
    ):
        """
        executing_orphan (subagent dispatched but write_result never received) must
        NOT consume execution_attempts budget — the subagent session was killed before
        the agent could complete or confirm work.

        Note: executing_orphan currently short-circuits to failed in _process_uow (4b-orphan
        path). This test verifies the correct behavior: no execution_attempts increment
        occurred before the short-circuit exit.
        """
        conn = _open_db(db_path)
        # executing_orphan uses a note-based startup_sweep entry
        orphan_entry = _make_startup_sweep_audit_entry("executing_orphan")
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            lifetime_cycles=1,
            retry_count=0,
            execution_attempts=0,
            audit_log_entries=[orphan_entry],
        )
        conn.close()

        with patch("src.orchestration.steward._send_escalation_notification"):
            _process_uow(
                uow=registry.get(uow_id),
                registry=registry,
                audit_entries=[orphan_entry],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["execution_attempts"] == 0, (
            f"execution_attempts must not increment on executing_orphan, got {row['execution_attempts']}"
        )


# ---------------------------------------------------------------------------
# Test: genuine execution failures DO consume execution_attempts
# ---------------------------------------------------------------------------

class TestGenuineExecutionConsumesRetryBudget:
    """
    When return_reason indicates a confirmed execution outcome (execution_complete,
    execution_failed), execution_attempts must increment.
    """

    def test_failed_execution_increments_execution_attempts(
        self, db_path, registry, tmp_path
    ):
        """
        When return_reason is execution_complete (outcome: failed), execution_attempts
        must increment — the agent ran and returned a result.
        """
        conn = _open_db(db_path)
        audit_entries = [
            {"event": "execution_complete", "return_reason": "failed",
             "from_status": "active", "to_status": "ready-for-steward"},
        ]
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            lifetime_cycles=1,
            retry_count=0,
            execution_attempts=0,
            audit_log_entries=audit_entries,
        )
        conn.close()

        with patch("src.orchestration.steward._send_escalation_notification"):
            _process_uow(
                uow=registry.get(uow_id),
                registry=registry,
                audit_entries=[{
                    "event": "execution_complete",
                    "return_reason": "failed",
                    "from_status": "active",
                    "to_status": "ready-for-steward",
                }],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["execution_attempts"] == 1, (
            f"execution_attempts must increment after a genuine execution failure, "
            f"got {row['execution_attempts']}"
        )

    def test_three_genuine_failures_exhaust_retry_cap(
        self, db_path, registry, tmp_path
    ):
        """
        When execution_attempts == MAX_RETRIES and return_reason is a genuine
        execution outcome, the UoW must escalate to needs-human-review.
        """
        conn = _open_db(db_path)
        audit_entries = [
            {"event": "execution_complete", "return_reason": "failed",
             "from_status": "active", "to_status": "ready-for-steward"},
        ]
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=MAX_RETRIES + 1,
            lifetime_cycles=MAX_RETRIES + 1,
            retry_count=MAX_RETRIES,
            execution_attempts=MAX_RETRIES,  # all retries were genuine executions
            audit_log_entries=audit_entries,
        )
        conn.close()

        escalation_calls = []

        with patch("src.orchestration.steward._send_escalation_notification",
                   side_effect=lambda uow: escalation_calls.append(True)):
            result = _process_uow(
                uow=registry.get(uow_id),
                registry=registry,
                audit_entries=[{
                    "event": "execution_complete",
                    "return_reason": "failed",
                    "from_status": "active",
                    "to_status": "ready-for-steward",
                }],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        assert escalation_calls, (
            "UoW must be escalated to needs-human-review when execution_attempts >= MAX_RETRIES "
            "and return_reason is a genuine execution outcome"
        )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["status"] == "needs-human-review", (
            f"Expected status=needs-human-review after {MAX_RETRIES} genuine execution failures, "
            f"got {row['status']}"
        )


# ---------------------------------------------------------------------------
# Test: retry_count continues to increment for diagnostic visibility
# ---------------------------------------------------------------------------

class TestRetryCountDiagnosticVisibility:
    """
    retry_count must continue to increment on every re-entry (regardless of
    return_reason) so that escalation notifications show total steward cycles
    for diagnostic purposes.
    """

    def test_retry_count_increments_on_orphan_return(
        self, db_path, registry, tmp_path
    ):
        """
        retry_count increments even on orphan returns — it is a diagnostic counter,
        not the retry budget gate.
        """
        conn = _open_db(db_path)
        orphan_entry = _make_startup_sweep_audit_entry("executor_orphan")
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            lifetime_cycles=1,
            retry_count=0,
            execution_attempts=0,
            audit_log_entries=[orphan_entry],
        )
        conn.close()

        with patch("src.orchestration.steward._send_escalation_notification"):
            _process_uow(
                uow=registry.get(uow_id),
                registry=registry,
                audit_entries=[orphan_entry],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["retry_count"] == 1, (
            f"retry_count must increment on orphan returns (diagnostic counter), "
            f"got {row['retry_count']}"
        )


# ---------------------------------------------------------------------------
# Test: mixed orphan + genuine execution scenario
# ---------------------------------------------------------------------------

class TestMixedOrphanAndGenuineExecution:
    """
    A UoW that sees both orphan events and genuine executions must gate
    MAX_RETRIES only on confirmed execution attempts.
    """

    def test_two_orphans_plus_one_failure_does_not_escalate(
        self, db_path, registry, tmp_path
    ):
        """
        2 orphan events + 1 genuine execution failure = execution_attempts=1.
        Must NOT escalate (execution_attempts < MAX_RETRIES).
        """
        conn = _open_db(db_path)
        # The most recent entry is execution_complete (failed) — this is a genuine execution
        failed_entry = {
            "event": "execution_complete",
            "return_reason": "failed",
            "from_status": "active",
            "to_status": "ready-for-steward",
        }
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=3,
            lifetime_cycles=3,
            retry_count=3,           # 2 orphans + 1 genuine = 3 steward cycles
            execution_attempts=1,    # only 1 confirmed execution
            audit_log_entries=[failed_entry],
        )
        conn.close()

        escalation_calls = []

        with patch("src.orchestration.steward._send_escalation_notification",
                   side_effect=lambda uow: escalation_calls.append(True)):
            _process_uow(
                uow=registry.get(uow_id),
                registry=registry,
                audit_entries=[failed_entry],
                issue_info=_make_issue_info(),
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=lambda uow, condition, **kwargs: None,
                llm_prescriber=_fake_llm_prescriber,
            )

        assert not escalation_calls, (
            "Must NOT escalate when execution_attempts=1 < MAX_RETRIES, even though "
            f"retry_count={MAX_RETRIES} (inflated by orphan events)"
        )

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["execution_attempts"] == 2, (
            f"execution_attempts must be 2 after one more genuine failure, "
            f"got {row['execution_attempts']}"
        )
