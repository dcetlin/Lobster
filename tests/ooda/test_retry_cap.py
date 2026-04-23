"""
tests/ooda/test_retry_cap.py

Unit tests for the WOS steward retry cap and escalation mechanism.

Coverage:
- When retry_count == MAX_RETRIES, steward transitions UoW to needs-human-review
  and calls send_escalation_notification instead of re-dispatching.
- When retry_count < MAX_RETRIES, steward increments retry_count and re-dispatches
  normally.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import MAX_RETRIES, _process_uow, _send_escalation_notification
from src.orchestration.registry import Registry, UoW, UoWStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
             retry_count, success_criteria, register, route_evidence, trigger,
             steward_agenda, steward_log)
        VALUES (?, 'executable', 'github:issue/99', 99, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}',
                NULL, NULL)
        """,
        (uow_id, status, now, now, summary, output_ref, steward_cycles,
         lifetime_cycles, retry_count, success_criteria, register),
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a path to a test DB. Registry will apply migrations when initialized."""
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Return a Registry instance — migrations are applied by Registry.__init__."""
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Test: MAX_RETRIES constant
# ---------------------------------------------------------------------------

class TestMaxRetriesConstant:
    def test_max_retries_is_three(self):
        """MAX_RETRIES must be 3 (spec-defined)."""
        assert MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# Test: retry_count increments below cap
# ---------------------------------------------------------------------------

class TestRetryCountIncrement:
    def test_steward_increments_retry_count_below_cap(self, db_path, registry, tmp_path):
        """
        When retry_count < MAX_RETRIES, the steward increments retry_count and
        re-dispatches the UoW (prescribes again).
        """
        conn = _open_db(db_path)
        # Audit entries simulating a previous failed execution (non-first-execution)
        audit_entries = [
            {"event": "execution_complete", "return_reason": "failed",
             "from_status": "active", "to_status": "ready-for-steward"},
        ]
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,  # cycles > 0 → re-dispatch path
            lifetime_cycles=1,
            retry_count=0,     # below MAX_RETRIES
            audit_log_entries=audit_entries,
            success_criteria="Some output created",
        )
        conn.close()

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.retry_count == 0

        notifications = []

        def fake_notify_dan(uow, condition, **kwargs):
            notifications.append(condition)

        # A minimal llm_prescriber that returns a valid prescription
        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(instructions="do something", success_criteria_check="output exists", estimated_cycles=1)

        from src.orchestration.steward import IssueInfo
        issue_info = IssueInfo(
            status_code=200, title="Test", body="", labels=[], state="open"
        )

        result = _process_uow(
            uow=uow,
            registry=registry,
            audit_entries=[{
                "event": "execution_complete",
                "return_reason": "failed",
                "from_status": "active",
                "to_status": "ready-for-steward",
            }],
            issue_info=issue_info,
            dry_run=False,
            artifact_dir=tmp_path,
            notify_dan=fake_notify_dan,
            llm_prescriber=fake_llm_prescriber,
        )

        # Should not have escalated — no needs-human-review notification
        assert "retry_cap" not in notifications

        # retry_count should be incremented in the DB
        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["retry_count"] == 1, (
            f"Expected retry_count=1 after one re-dispatch, got {row['retry_count']}"
        )

    def test_steward_increments_retry_count_at_max_minus_one(self, db_path, registry, tmp_path):
        """
        When retry_count == MAX_RETRIES - 1, steward increments to MAX_RETRIES
        and still re-dispatches (cap not exceeded yet).
        """
        conn = _open_db(db_path)
        audit_entries = [
            {"event": "execution_complete", "return_reason": "failed",
             "from_status": "active", "to_status": "ready-for-steward"},
        ]
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=MAX_RETRIES,
            lifetime_cycles=MAX_RETRIES,
            retry_count=MAX_RETRIES - 1,  # one below cap
            audit_log_entries=audit_entries,
        )
        conn.close()

        uow = registry.get(uow_id)
        notifications = []

        def fake_notify_dan(uow, condition, **kwargs):
            notifications.append(condition)

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(instructions="do something", success_criteria_check="output exists", estimated_cycles=1)

        from src.orchestration.steward import IssueInfo
        issue_info = IssueInfo(status_code=200, title="Test", body="", labels=[], state="open")

        result = _process_uow(
            uow=uow,
            registry=registry,
            audit_entries=[{
                "event": "execution_complete",
                "return_reason": "failed",
                "from_status": "active",
                "to_status": "ready-for-steward",
            }],
            issue_info=issue_info,
            dry_run=False,
            artifact_dir=tmp_path,
            notify_dan=fake_notify_dan,
            llm_prescriber=fake_llm_prescriber,
        )

        # No escalation yet — still one more retry allowed
        assert "retry_cap" not in notifications

        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        # Should have incremented from MAX_RETRIES-1 to MAX_RETRIES
        assert row["retry_count"] == MAX_RETRIES, (
            f"Expected retry_count={MAX_RETRIES}, got {row['retry_count']}"
        )


# ---------------------------------------------------------------------------
# Test: retry cap triggers needs-human-review
# ---------------------------------------------------------------------------

class TestRetryCapEscalation:
    def test_steward_escalates_when_retry_count_at_cap(self, db_path, registry, tmp_path):
        """
        When retry_count == MAX_RETRIES, the steward must:
        1. Transition the UoW to needs-human-review.
        2. Call _send_escalation_notification (not re-dispatch).
        3. NOT increment retry_count further.
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
            retry_count=MAX_RETRIES,  # at cap
            audit_log_entries=audit_entries,
        )
        conn.close()

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.retry_count == MAX_RETRIES

        escalation_calls = []
        prescribe_calls = []
        notify_calls = []

        def fake_notify_dan(uow, condition, **kwargs):
            notify_calls.append(condition)

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            prescribe_calls.append(True)
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(instructions="do something", success_criteria_check="output exists", estimated_cycles=1)

        from src.orchestration.steward import IssueInfo

        # Patch _send_escalation_notification to capture the call without side effects
        with patch("src.orchestration.steward._send_escalation_notification") as mock_escalate:
            issue_info = IssueInfo(status_code=200, title="Test", body="", labels=[], state="open")

            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[{
                    "event": "execution_complete",
                    "return_reason": "failed",
                    "from_status": "active",
                    "to_status": "ready-for-steward",
                }],
                issue_info=issue_info,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

            # _send_escalation_notification must have been called
            assert mock_escalate.called, (
                "_send_escalation_notification must be called when retry cap is exceeded"
            )

        # LLM prescriber must NOT have been called (we escalated, not re-dispatched)
        assert not prescribe_calls, (
            "LLM prescriber must not be called when retry cap is exceeded"
        )

        # UoW status must be needs-human-review
        conn2 = _open_db(db_path)
        row = _get_uow_row(conn2, uow_id)
        conn2.close()
        assert row["status"] == "needs-human-review", (
            f"Expected status=needs-human-review, got {row['status']}"
        )
        # retry_count must NOT have been incremented
        assert row["retry_count"] == MAX_RETRIES, (
            f"Expected retry_count={MAX_RETRIES} (not incremented), got {row['retry_count']}"
        )

    def test_steward_does_not_escalate_on_first_execution(self, db_path, registry, tmp_path):
        """
        On cycles == 0 (first execution), retry cap must not fire regardless of
        retry_count field value. The cap only applies to re-dispatches.
        """
        conn = _open_db(db_path)
        # No audit entries → first_execution posture
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            lifetime_cycles=0,
            retry_count=MAX_RETRIES,  # pre-set high (shouldn't matter on cycle 0)
        )
        conn.close()

        uow = registry.get(uow_id)
        notify_calls = []

        def fake_notify_dan(uow, condition, **kwargs):
            notify_calls.append(condition)

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(instructions="do something", success_criteria_check="output exists", estimated_cycles=1)

        from src.orchestration.steward import IssueInfo

        with patch("src.orchestration.steward._send_escalation_notification") as mock_escalate:
            issue_info = IssueInfo(status_code=200, title="Test", body="", labels=[], state="open")
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=[],  # no prior execution
                issue_info=issue_info,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

            # Escalation must NOT have been called on first execution
            assert not mock_escalate.called, (
                "_send_escalation_notification must NOT be called on first execution (cycles=0)"
            )

    def test_send_escalation_notification_writes_inbox_message(self, tmp_path):
        """
        _send_escalation_notification must write a JSON file to ~/messages/inbox/.
        It does NOT raise on normal operation.
        """
        uow = UoW(
            id="uow_test_aabbcc",
            status=UoWStatus.DIAGNOSING,
            summary="Remove dead code",
            source="github:issue/42",
            source_issue_number=42,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            retry_count=MAX_RETRIES,
        )

        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()

        with patch("src.orchestration.steward.Path") as mock_path_cls:
            # Only intercept the inbox path construction
            real_path = Path
            def path_side_effect(*args):
                if args and "messages/inbox" in str(args[0] if args else ""):
                    return fake_inbox
                return real_path(*args)
            mock_path_cls.side_effect = path_side_effect

            # Call directly — should not raise
            _send_escalation_notification(uow)

        # There must be at least one JSON file written
        written = list(fake_inbox.glob("*.json"))
        assert written, "Expected at least one inbox JSON file written by escalation notification"

        data = json.loads(written[0].read_text())
        assert data.get("metadata", {}).get("condition") == "retry_cap"
        assert uow.id in data["text"]


# ---------------------------------------------------------------------------
# Test: UoWStatus.NEEDS_HUMAN_REVIEW
# ---------------------------------------------------------------------------

class TestNeedsHumanReviewStatus:
    def test_needs_human_review_status_exists(self):
        """UoWStatus must have NEEDS_HUMAN_REVIEW = 'needs-human-review'."""
        assert UoWStatus.NEEDS_HUMAN_REVIEW == "needs-human-review"

    def test_needs_human_review_is_not_terminal(self):
        """needs-human-review is not a terminal status (does not allow re-proposal)."""
        assert not UoWStatus.NEEDS_HUMAN_REVIEW.is_terminal()

    def test_needs_human_review_is_not_in_flight(self):
        """
        needs-human-review is not in the is_in_flight set.
        It is a parking state — not actively executing or awaiting steward.
        """
        assert not UoWStatus.NEEDS_HUMAN_REVIEW.is_in_flight()
