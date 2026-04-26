"""
tests/ooda/test_escalation_consolidation.py

Unit tests for shared-cause escalation consolidation.

Spec (architectural-proposal-20260426.md §Category 4 / §3):
  - When N UoWs escalate within the same heartbeat cycle, all via the same
    return_reason, produce ONE consolidated Telegram notification rather than N
    individual ones.
  - Individual UoW records still transition to needs-human-review; only the
    notification layer is consolidated.
  - The consolidation threshold is ESCALATION_CONSOLIDATION_THRESHOLD. At or
    above that count: one consolidated message. Below: individual messages per UoW.
  - The consolidated message must include: count of affected UoWs, the shared
    return_reason, and each UoW's ID.

Coverage:
- ESCALATION_CONSOLIDATION_THRESHOLD constant is exported from steward.
- _build_consolidated_escalation_text: pure function, no side effects.
  - Returns a string mentioning the count and the shared cause.
  - Includes each UoW ID in the output.
  - Works correctly at exactly the threshold boundary.
- _send_consolidated_escalation_notification: writes one inbox JSON file.
- run_steward_cycle: when >= ESCALATION_CONSOLIDATION_THRESHOLD UoWs escalate
  in the same cycle, only ONE inbox notification is written (not N).
- run_steward_cycle: when < ESCALATION_CONSOLIDATION_THRESHOLD UoWs escalate,
  individual notifications are written per UoW.
- All individual UoW records transition to needs-human-review regardless of
  whether notification is consolidated.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    ESCALATION_CONSOLIDATION_THRESHOLD,
    MAX_RETRIES,
    _build_consolidated_escalation_text,
    _send_consolidated_escalation_notification,
    EscalationRecord,
    run_steward_cycle,
)
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


_ISSUE_COUNTER: list[int] = [1000]  # mutable singleton to generate unique issue numbers


def _make_uow_at_retry_cap(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    return_reason: str = "executor_orphan",
    summary: str = "Test UoW",
) -> str:
    """
    Insert a UoW at the retry cap boundary (retry_count == MAX_RETRIES,
    steward_cycles > 0, with a prior execution_complete audit entry for the
    given return_reason).
    Returns the uow_id.
    """
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    # Each UoW needs a unique (source_issue_number, sweep_date) due to the DB constraint.
    # Use a monotonically incrementing issue number and a unique date suffix.
    _ISSUE_COUNTER[0] += 1
    issue_num = _ISSUE_COUNTER[0]
    sweep_date = f"2026-{(issue_num % 12) + 1:02d}-{(issue_num % 28) + 1:02d}"
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles, lifetime_cycles,
             retry_count, success_criteria, register, route_evidence, trigger,
             steward_agenda, steward_log)
        VALUES (?, 'executable', ?, ?, ?, ?, 'solo',
                ?, ?, ?, NULL, ?, ?, ?, ?, 'operational', '{}', '{"type": "immediate"}',
                NULL, NULL)
        """,
        (
            uow_id,
            f"github:issue/{issue_num}",
            issue_num,
            sweep_date,
            "ready-for-steward",
            now, now,
            summary,
            MAX_RETRIES + 1,  # steward_cycles > 0 → re-dispatch path
            MAX_RETRIES + 1,  # lifetime_cycles
            MAX_RETRIES,      # retry_count == MAX_RETRIES → cap exceeded on next increment
            "Output exists",
        ),
    )
    # Audit entry that makes _most_recent_return_reason return our return_reason
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now, uow_id, "execution_complete",
            "active", "ready-for-steward", "executor",
            json.dumps({"event": "execution_complete", "return_reason": return_reason}),
        ),
    )
    conn.commit()
    return uow_id


def _get_uow_status(conn: sqlite3.Connection, uow_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    return row["status"] if row else None


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
# Test: constant exists and has expected value
# ---------------------------------------------------------------------------

class TestEscalationConsolidationThreshold:
    def test_threshold_is_exported(self):
        """ESCALATION_CONSOLIDATION_THRESHOLD must be exported from steward."""
        assert ESCALATION_CONSOLIDATION_THRESHOLD is not None

    def test_threshold_is_at_least_two(self):
        """
        Threshold must be >= 2. A threshold of 1 would consolidate every single
        escalation, defeating the purpose.
        """
        assert ESCALATION_CONSOLIDATION_THRESHOLD >= 2

    def test_threshold_is_an_integer(self):
        assert isinstance(ESCALATION_CONSOLIDATION_THRESHOLD, int)


# ---------------------------------------------------------------------------
# Test: EscalationRecord dataclass
# ---------------------------------------------------------------------------

class TestEscalationRecord:
    def test_escalation_record_carries_uow_and_return_reason(self):
        """EscalationRecord must hold a UoW and a return_reason string."""
        uow = UoW(
            id="uow_test_abc123",
            status=UoWStatus.DIAGNOSING,
            summary="Some work",
            source="github:issue/1",
            source_issue_number=1,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        rec = EscalationRecord(uow=uow, return_reason="executor_orphan")
        assert rec.uow is uow
        assert rec.return_reason == "executor_orphan"


# ---------------------------------------------------------------------------
# Test: _build_consolidated_escalation_text (pure function)
# ---------------------------------------------------------------------------

class TestBuildConsolidatedEscalationText:
    def _make_records(self, count: int, return_reason: str = "executor_orphan") -> list[EscalationRecord]:
        records = []
        for i in range(count):
            uow = UoW(
                id=f"uow_test_{i:04d}",
                status=UoWStatus.NEEDS_HUMAN_REVIEW,
                summary=f"Work item {i}",
                source="github:issue/1",
                source_issue_number=1,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
            records.append(EscalationRecord(uow=uow, return_reason=return_reason))
        return records

    def test_output_mentions_count(self):
        """Consolidated text must state the number of affected UoWs."""
        records = self._make_records(5)
        text = _build_consolidated_escalation_text(records)
        assert "5" in text

    def test_output_mentions_shared_cause(self):
        """Consolidated text must name the shared return_reason."""
        records = self._make_records(3, return_reason="executor_orphan")
        text = _build_consolidated_escalation_text(records)
        assert "executor_orphan" in text

    def test_output_includes_each_uow_id(self):
        """Consolidated text must include every affected UoW ID."""
        records = self._make_records(4)
        text = _build_consolidated_escalation_text(records)
        for rec in records:
            assert rec.uow.id in text, f"Expected {rec.uow.id} in consolidated text"

    def test_pure_function_no_side_effects(self, tmp_path):
        """
        _build_consolidated_escalation_text must not write files, write to DB,
        or produce observable side effects. Called twice with the same input,
        it must return the same output.
        """
        records = self._make_records(3)
        text1 = _build_consolidated_escalation_text(records)
        text2 = _build_consolidated_escalation_text(records)
        assert text1 == text2

    def test_at_threshold_boundary(self):
        """Text generation works at exactly ESCALATION_CONSOLIDATION_THRESHOLD records."""
        records = self._make_records(ESCALATION_CONSOLIDATION_THRESHOLD)
        text = _build_consolidated_escalation_text(records)
        assert str(ESCALATION_CONSOLIDATION_THRESHOLD) in text

    def test_heterogeneous_return_reasons_listed(self):
        """
        When records have different return_reasons (mixed-cause wave), each cause
        should appear in the consolidated text or the text should clearly reflect
        the mixed nature.
        """
        uow_a = UoW(
            id="uow_a", status=UoWStatus.NEEDS_HUMAN_REVIEW,
            summary="A", source="github:issue/1", source_issue_number=1,
            created_at=_now_iso(), updated_at=_now_iso(),
        )
        uow_b = UoW(
            id="uow_b", status=UoWStatus.NEEDS_HUMAN_REVIEW,
            summary="B", source="github:issue/2", source_issue_number=2,
            created_at=_now_iso(), updated_at=_now_iso(),
        )
        records = [
            EscalationRecord(uow=uow_a, return_reason="executor_orphan"),
            EscalationRecord(uow=uow_b, return_reason="executing_orphan"),
        ]
        text = _build_consolidated_escalation_text(records)
        # Should list both IDs regardless of cause grouping
        assert "uow_a" in text
        assert "uow_b" in text


# ---------------------------------------------------------------------------
# Test: _send_consolidated_escalation_notification writes one inbox file
# ---------------------------------------------------------------------------

class TestSendConsolidatedEscalationNotification:
    def _make_records(self, count: int, return_reason: str = "executor_orphan") -> list[EscalationRecord]:
        records = []
        for i in range(count):
            uow = UoW(
                id=f"uow_test_{i:04d}",
                status=UoWStatus.NEEDS_HUMAN_REVIEW,
                summary=f"Work item {i}",
                source="github:issue/1",
                source_issue_number=1,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
            records.append(EscalationRecord(uow=uow, return_reason=return_reason))
        return records

    def test_writes_exactly_one_inbox_file(self, tmp_path):
        """
        _send_consolidated_escalation_notification must write exactly one JSON
        file to the inbox directory, regardless of how many UoWs are in the batch.
        """
        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()
        records = self._make_records(5)

        with patch("src.orchestration.steward.Path") as mock_path_cls:
            real_path = Path

            def path_side_effect(*args):
                if args and "messages/inbox" in str(args[0] if args else ""):
                    return fake_inbox
                return real_path(*args)

            mock_path_cls.side_effect = path_side_effect
            _send_consolidated_escalation_notification(records)

        written = list(fake_inbox.glob("*.json"))
        assert len(written) == 1, (
            f"Expected exactly 1 inbox file, got {len(written)}"
        )

    def test_inbox_file_has_correct_metadata(self, tmp_path):
        """
        The written inbox file must have metadata indicating it is a consolidated
        escalation signal, not an individual one.
        """
        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()
        records = self._make_records(3, return_reason="executor_orphan")

        with patch("src.orchestration.steward.Path") as mock_path_cls:
            real_path = Path

            def path_side_effect(*args):
                if args and "messages/inbox" in str(args[0] if args else ""):
                    return fake_inbox
                return real_path(*args)

            mock_path_cls.side_effect = path_side_effect
            _send_consolidated_escalation_notification(records)

        written = list(fake_inbox.glob("*.json"))
        assert written
        data = json.loads(written[0].read_text())
        metadata = data.get("metadata", {})
        assert metadata.get("condition") == "retry_cap_consolidated"
        assert metadata.get("escalation_count") == 3


# ---------------------------------------------------------------------------
# Test: run_steward_cycle consolidation behavior
# ---------------------------------------------------------------------------

class TestRunStewardCycleConsolidation:
    """
    Integration-level tests that run the full Steward cycle against a test
    registry and verify the notification behavior at the cycle level.
    """

    def _make_uow_at_cap(
        self,
        conn: sqlite3.Connection,
        return_reason: str = "executor_orphan",
        summary: str = "Test UoW",
    ) -> str:
        return _make_uow_at_retry_cap(conn, return_reason=return_reason, summary=summary)

    def test_individual_notifications_below_threshold(self, db_path, registry, tmp_path):
        """
        When fewer than ESCALATION_CONSOLIDATION_THRESHOLD UoWs hit the retry cap
        in a single cycle, each should trigger its own individual notification.
        """
        count = ESCALATION_CONSOLIDATION_THRESHOLD - 1
        conn = _open_db(db_path)
        uow_ids = [
            self._make_uow_at_cap(conn, return_reason="executor_orphan")
            for _ in range(count)
        ]
        conn.close()

        individual_calls = []
        consolidated_calls = []

        def fake_notify_dan(uow, condition, **kwargs):
            pass

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(
                instructions="do something",
                success_criteria_check="output exists",
                estimated_cycles=1,
            )

        with (
            patch("src.orchestration.steward._send_escalation_notification") as mock_individual,
            patch("src.orchestration.steward._send_consolidated_escalation_notification") as mock_consolidated,
        ):
            run_steward_cycle(
                registry=registry,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

        # Individual escalation must have been called once per UoW
        assert mock_individual.call_count == count, (
            f"Expected {count} individual escalation calls, got {mock_individual.call_count}"
        )
        # Consolidated must NOT have been called
        assert mock_consolidated.call_count == 0, (
            f"Consolidated notification must not fire below threshold, "
            f"got {mock_consolidated.call_count} calls"
        )

    def test_consolidated_notification_at_threshold(self, db_path, registry, tmp_path):
        """
        When exactly ESCALATION_CONSOLIDATION_THRESHOLD UoWs hit the retry cap
        in the same cycle, ONE consolidated notification is sent and zero individual
        notifications are sent.
        """
        count = ESCALATION_CONSOLIDATION_THRESHOLD
        conn = _open_db(db_path)
        uow_ids = [
            self._make_uow_at_cap(conn, return_reason="executor_orphan")
            for _ in range(count)
        ]
        conn.close()

        def fake_notify_dan(uow, condition, **kwargs):
            pass

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(
                instructions="do something",
                success_criteria_check="output exists",
                estimated_cycles=1,
            )

        with (
            patch("src.orchestration.steward._send_escalation_notification") as mock_individual,
            patch("src.orchestration.steward._send_consolidated_escalation_notification") as mock_consolidated,
        ):
            run_steward_cycle(
                registry=registry,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

        # Individual escalation must NOT have been called
        assert mock_individual.call_count == 0, (
            f"Individual escalation must be suppressed when consolidating, "
            f"got {mock_individual.call_count} calls"
        )
        # Exactly one consolidated call
        assert mock_consolidated.call_count == 1, (
            f"Expected 1 consolidated escalation call, got {mock_consolidated.call_count}"
        )

    def test_consolidated_notification_above_threshold(self, db_path, registry, tmp_path):
        """
        When more than ESCALATION_CONSOLIDATION_THRESHOLD UoWs escalate, still
        ONE consolidated notification (not multiple consolidated or hybrid).
        """
        count = ESCALATION_CONSOLIDATION_THRESHOLD + 4
        conn = _open_db(db_path)
        for _ in range(count):
            self._make_uow_at_cap(conn, return_reason="executor_orphan")
        conn.close()

        def fake_notify_dan(uow, condition, **kwargs):
            pass

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(
                instructions="do something",
                success_criteria_check="output exists",
                estimated_cycles=1,
            )

        with (
            patch("src.orchestration.steward._send_escalation_notification") as mock_individual,
            patch("src.orchestration.steward._send_consolidated_escalation_notification") as mock_consolidated,
        ):
            run_steward_cycle(
                registry=registry,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

        assert mock_individual.call_count == 0, (
            f"No individual notifications when above threshold, "
            f"got {mock_individual.call_count}"
        )
        assert mock_consolidated.call_count == 1, (
            f"Expected exactly 1 consolidated notification, got {mock_consolidated.call_count}"
        )

    def test_all_uows_transition_to_needs_human_review_regardless_of_consolidation(
        self, db_path, registry, tmp_path
    ):
        """
        Whether notification is individual or consolidated, every UoW that hits
        the retry cap must transition to needs-human-review in the registry.
        Individual records must not be suppressed — only the notification is consolidated.
        """
        count = ESCALATION_CONSOLIDATION_THRESHOLD + 2
        conn = _open_db(db_path)
        uow_ids = [
            self._make_uow_at_cap(conn, return_reason="executor_orphan")
            for _ in range(count)
        ]
        conn.close()

        def fake_notify_dan(uow, condition, **kwargs):
            pass

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(
                instructions="do something",
                success_criteria_check="output exists",
                estimated_cycles=1,
            )

        with (
            patch("src.orchestration.steward._send_escalation_notification"),
            patch("src.orchestration.steward._send_consolidated_escalation_notification"),
        ):
            run_steward_cycle(
                registry=registry,
                dry_run=False,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

        conn2 = _open_db(db_path)
        for uow_id in uow_ids:
            status = _get_uow_status(conn2, uow_id)
            assert status == "needs-human-review", (
                f"UoW {uow_id} must be in needs-human-review, got {status!r}"
            )
        conn2.close()

    def test_dry_run_does_not_send_any_notification(self, db_path, registry, tmp_path):
        """
        In dry_run mode, no notifications of any kind should be sent —
        neither individual nor consolidated.
        """
        count = ESCALATION_CONSOLIDATION_THRESHOLD + 2
        conn = _open_db(db_path)
        for _ in range(count):
            self._make_uow_at_cap(conn, return_reason="executor_orphan")
        conn.close()

        def fake_notify_dan(uow, condition, **kwargs):
            pass

        def fake_llm_prescriber(uow_arg, reentry_posture, completion_gap, issue_body=""):
            from src.orchestration.steward import LLMPrescription
            return LLMPrescription(
                instructions="do something",
                success_criteria_check="output exists",
                estimated_cycles=1,
            )

        with (
            patch("src.orchestration.steward._send_escalation_notification") as mock_individual,
            patch("src.orchestration.steward._send_consolidated_escalation_notification") as mock_consolidated,
        ):
            run_steward_cycle(
                registry=registry,
                dry_run=True,
                artifact_dir=tmp_path,
                notify_dan=fake_notify_dan,
                llm_prescriber=fake_llm_prescriber,
            )

        assert mock_individual.call_count == 0, (
            f"No individual notifications in dry_run, got {mock_individual.call_count}"
        )
        assert mock_consolidated.call_count == 0, (
            f"No consolidated notifications in dry_run, got {mock_consolidated.call_count}"
        )
