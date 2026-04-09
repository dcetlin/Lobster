"""
Integration test: WOS re-prescription cycle.

Validates that when an Executor subagent writes result.json with
status=failed, the Steward:
  1. Reads the failure outcome
  2. Re-prescribes (unless steward_cycles >= 5 cap)
  3. Returns the UoW to ready-for-executor for another attempt
  4. Increments steward_cycles on each re-prescription pass
  5. Surfaces to Dan (sets status=blocked) at steward_cycles == 5 without re-prescribing

Test structure:
  - In-memory SQLite DB with migrations applied
  - Mocked LLM calls (Steward github_client + artifact_dir injection)
  - Mocked Executor dispatcher (no real subagent spawned)
  - No network I/O; all file writes in tmp_path

State machine exercised:
  pending
    → ready-for-steward (via set_status_direct)
    → diagnosing → ready-for-executor  [Steward cycle 1, steward_cycles=1]
    → active                            [Executor claim]
    → ready-for-steward                 [result.json outcome=failed written]
    → diagnosing → ready-for-executor  [Steward cycle 2, steward_cycles=2]
    → active → ready-for-steward        [Executor fails again]
    → diagnosing → ready-for-executor  [Steward cycle 3, steward_cycles=3]
    → active → ready-for-steward        [Executor fails again]
    → diagnosing → ready-for-executor  [Steward cycle 4, steward_cycles=4]
    → active → ready-for-steward        [Executor fails again]
    → diagnosing → blocked              [Steward cycle 5 — surfaces to Dan]
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest
import sys

# ---------------------------------------------------------------------------
# Path setup — repo root → src
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations
from orchestration.registry import Registry, UpsertInserted, ApproveConfirmed
from orchestration.steward import (
    run_steward_cycle,
    _HARD_CAP_CYCLES,
    _EARLY_WARNING_CYCLES,
)
from orchestration.executor import Executor, ExecutorOutcome, _result_json_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_uow_row(conn: sqlite3.Connection, uow_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    assert row is not None, f"UoW {uow_id!r} not found"
    return dict(row)


def _read_audit_log(conn: sqlite3.Connection, uow_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
        (uow_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _stub_github_client(issue_number: int) -> dict[str, Any]:
    """No-op GitHub client — returns open issue with no labels."""
    return {
        "status_code": 200,
        "state": "open",
        "labels": [],
        "body": f"Test issue #{issue_number}",
        "title": f"Test issue #{issue_number}",
    }


def _seed_uow_at_ready_for_steward(
    registry: Registry,
    conn: sqlite3.Connection,
    issue_number: int,
    title: str,
    success_criteria: str,
) -> str:
    """
    Create a UoW and advance it to ready-for-steward in one step.

    Returns the uow_id.
    """
    # Insert as proposed
    result = registry.upsert(issue_number=issue_number, title=title, success_criteria="Test completion.")
    assert isinstance(result, UpsertInserted), f"Expected UpsertInserted, got: {result}"
    uow_id = result.id

    # proposed → pending
    approve_result = registry.approve(uow_id)
    assert isinstance(approve_result, ApproveConfirmed), (
        f"Expected ApproveConfirmed, got: {approve_result}"
    )

    # Set success_criteria directly (Registrar would do this in production)
    conn.execute(
        "UPDATE uow_registry SET success_criteria = ?, updated_at = ? WHERE id = ?",
        (success_criteria, _now(), uow_id),
    )
    conn.commit()

    # pending → ready-for-steward
    registry.set_status_direct(uow_id, "ready-for-steward")

    return uow_id


def _simulate_executor_fail(
    registry: Registry,
    conn: sqlite3.Connection,
    uow_id: str,
    tmp_path: Path,
    fail_reason: str = "subagent encountered an error",
) -> None:
    """
    Simulate a subagent Executor that fails.

    Steps:
    1. Claim the UoW (ready-for-executor → active) using the real Executor.
    2. Overwrite the result.json that Executor wrote (complete) with a failed result.
    3. Transition status back to ready-for-steward (mimics the Executor returning
       after the subagent wrote a failed result).

    This simulates the production path where the subagent writes result.json with
    outcome=failed and the Executor transitions back to ready-for-steward.
    """
    # Use real Executor with a noop dispatcher that just returns an executor_id.
    # This exercises the full 6-step claim sequence and writes a result.json with
    # outcome=complete, then we overwrite it to simulate a subagent failure.
    def _noop_dispatcher(instructions: str, uow_id: str) -> str:
        return f"test-executor-{uow_id[:8]}"

    executor = Executor(registry, dispatcher=_noop_dispatcher)
    executor_result = executor.execute_uow(uow_id)

    # Overwrite the result.json with a failed outcome (simulating what a real
    # subagent would write after failing).
    output_ref = executor_result.output_artifact or conn.execute(
        "SELECT output_ref FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()["output_ref"]

    failed_result = {
        "uow_id": uow_id,
        "outcome": "failed",
        "success": False,
        "reason": fail_reason,
    }
    result_path = _result_json_path(output_ref)
    result_path.write_text(json.dumps(failed_result), encoding="utf-8")

    # Overwrite the output_ref content to be non-empty (Executor already wrote it)
    # so output_ref_is_valid() returns True in the Steward.
    # (Executor already wrote to output_ref in _run_execution — so it exists.)

    # Add execution_failed audit entry so the Steward sees the failure context.
    # In production, the subagent writes this; here we inject it directly.
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, 'execution_failed', 'active', 'ready-for-steward', 'executor-test',
                ?)
        """,
        (
            _now(),
            uow_id,
            json.dumps({
                "return_reason": "execution_failed",
                "classification": "error",
                "reason": fail_reason,
            }),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    """Fully-migrated Registry in a temp directory."""
    db_path = tmp_path / "wos_represcription_test.db"
    run_migrations(db_path)
    return Registry(db_path)


@pytest.fixture
def conn(registry: Registry):
    """Direct DB connection for assertions. Closed after test."""
    c = sqlite3.connect(str(registry.db_path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestReprescriptionCycle:
    """
    Re-prescription cycle: Executor failure → Steward re-prescribes.

    Covers:
    - steward_cycles increments on each pass
    - status returns to ready-for-executor (not done, not blocked) below cap
    - At steward_cycles == 5 (HARD_CAP), Steward surfaces to Dan (status=blocked)
      without re-prescribing
    """

    def test_single_failure_and_represcription(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        One failure cycle: Steward prescribes, Executor fails, Steward re-prescribes.

        Post-condition:
          - status == ready-for-executor
          - steward_cycles == 2
        """
        # Capture notifications to Dan (should NOT fire below cap)
        dan_calls: list[tuple] = []

        def mock_notify_dan(uow, condition, surface_log=None, return_reason=None):
            dan_calls.append((uow.id, condition))

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7001,
            title="Test single-failure represcription",
            success_criteria="Output file contains 'success'.",
        )

        # --- Steward cycle 1: pending → ready-for-executor (first prescription)
        result1 = run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=mock_notify_dan,
            bootup_candidate_gate=False,
        )
        assert result1.prescribed == 1, f"Expected 1 prescribed, got: {result1}"
        assert result1.surfaced == 0

        row1 = _read_uow_row(conn, uow_id)
        assert row1["status"] == "ready-for-executor", (
            f"After first Steward cycle, expected ready-for-executor, got: {row1['status']}"
        )
        assert row1["steward_cycles"] == 1, (
            f"Expected steward_cycles=1, got: {row1['steward_cycles']}"
        )

        # --- Executor cycle 1: Executor claims + subagent fails
        _simulate_executor_fail(registry, conn, uow_id, tmp_path, "first failure")

        # Steward must see ready-for-steward after Executor returns
        row_after_exec1 = _read_uow_row(conn, uow_id)
        assert row_after_exec1["status"] == "ready-for-steward", (
            f"After first executor fail, expected ready-for-steward, "
            f"got: {row_after_exec1['status']}"
        )

        # --- Steward cycle 2: re-prescribes because outcome=failed
        result2 = run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=mock_notify_dan,
            bootup_candidate_gate=False,
        )
        assert result2.prescribed == 1, f"Expected 1 re-prescribed, got: {result2}"
        assert result2.surfaced == 0, "Should not surface below cap"

        row2 = _read_uow_row(conn, uow_id)
        assert row2["status"] == "ready-for-executor", (
            f"After re-prescription, expected ready-for-executor, got: {row2['status']}"
        )
        assert row2["steward_cycles"] == 2, (
            f"Expected steward_cycles=2, got: {row2['steward_cycles']}"
        )

        assert len(dan_calls) == 0, f"Expected no Dan notifications, got: {dan_calls}"

    def test_steward_cycles_increment_across_multiple_failures(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        Three failure cycles in sequence: steward_cycles increments correctly.

        Post-condition after cycle 3:
          - status == ready-for-executor
          - steward_cycles == 3
        """
        dan_calls: list[tuple] = []

        def mock_notify_dan(uow, condition, surface_log=None, return_reason=None):
            dan_calls.append((uow.id, condition))

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7002,
            title="Test multi-failure steward_cycles increment",
            success_criteria="Task output written and verified.",
        )

        for cycle_num in range(1, 4):
            # Steward prescribes
            steward_result = run_steward_cycle(
                registry=registry,
                github_client=_stub_github_client,
                artifact_dir=artifact_dir,
                notify_dan=mock_notify_dan,
                bootup_candidate_gate=False,
            )
            assert steward_result.prescribed == 1, (
                f"Cycle {cycle_num}: expected prescribed=1, got: {steward_result}"
            )

            row = _read_uow_row(conn, uow_id)
            assert row["status"] == "ready-for-executor", (
                f"Cycle {cycle_num}: expected ready-for-executor after Steward, "
                f"got: {row['status']}"
            )
            assert row["steward_cycles"] == cycle_num, (
                f"Cycle {cycle_num}: expected steward_cycles={cycle_num}, "
                f"got: {row['steward_cycles']}"
            )

            # Executor fails (unless we've reached the cap, which won't happen here)
            _simulate_executor_fail(
                registry, conn, uow_id, tmp_path, f"failure #{cycle_num}"
            )

        # After 3 executor failures and 3 Steward re-prescriptions, steward_cycles == 3
        row_final = _read_uow_row(conn, uow_id)
        assert row_final["steward_cycles"] == 3
        assert row_final["status"] == "ready-for-steward"

        assert len(dan_calls) == 0, (
            f"No Dan notifications expected below cap, got: {dan_calls}"
        )

    def test_hard_cap_surfaces_to_dan_and_does_not_represcribe(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        At steward_cycles == _HARD_CAP_CYCLES (5), Steward surfaces to Dan.

        The hard cap fires when the UoW enters a Steward cycle with
        steward_cycles already equal to _HARD_CAP_CYCLES. That means:
          - 5 Steward prescriptions occur (steward_cycles 1..5)
          - 5 Executor failures return the UoW to ready-for-steward
          - On the 6th Steward run, cycles=5 >= 5 → surface, not prescribe

        Invariants:
          - status transitions to blocked (not ready-for-executor)
          - steward_cycles does NOT increment when surfacing (no new prescription)
          - notify_dan is called exactly once with condition='hard_cap'
        """
        dan_calls: list[tuple] = []
        early_warning_calls: list[tuple] = []

        def mock_notify_dan(uow, condition, surface_log=None, return_reason=None):
            dan_calls.append((uow.id, condition))

        def mock_notify_dan_early_warning(uow, return_reason, new_cycles=None):
            early_warning_calls.append((uow.id, new_cycles))

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7003,
            title="Test hard cap surfaces to Dan",
            success_criteria="Hard cap test — should surface at cycle 5.",
        )

        # Run _HARD_CAP_CYCLES fail cycles (steward_cycles will reach _HARD_CAP_CYCLES)
        for cycle_num in range(1, _HARD_CAP_CYCLES + 1):
            steward_result = run_steward_cycle(
                registry=registry,
                github_client=_stub_github_client,
                artifact_dir=artifact_dir,
                notify_dan=mock_notify_dan,
                notify_dan_early_warning=mock_notify_dan_early_warning,
                bootup_candidate_gate=False,
            )
            assert steward_result.prescribed == 1, (
                f"Cycle {cycle_num}: expected prescribed=1, got: {steward_result}"
            )

            row = _read_uow_row(conn, uow_id)
            assert row["steward_cycles"] == cycle_num, (
                f"Cycle {cycle_num}: expected steward_cycles={cycle_num}, "
                f"got: {row['steward_cycles']}"
            )

            _simulate_executor_fail(
                registry, conn, uow_id, tmp_path, f"failure #{cycle_num}"
            )

        # At this point, steward_cycles == _HARD_CAP_CYCLES (5), status=ready-for-steward.
        # The next Steward run should surface to Dan, NOT re-prescribe.
        row_before_cap = _read_uow_row(conn, uow_id)
        assert row_before_cap["steward_cycles"] == _HARD_CAP_CYCLES, (
            f"Expected steward_cycles={_HARD_CAP_CYCLES} before cap run, "
            f"got: {row_before_cap['steward_cycles']}"
        )
        assert row_before_cap["status"] == "ready-for-steward"

        # --- Steward run at cap: should surface to Dan, not re-prescribe
        cap_result = run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=mock_notify_dan,
            notify_dan_early_warning=mock_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        assert cap_result.surfaced == 1, (
            f"Expected surfaced=1 at hard cap, got: {cap_result}"
        )
        assert cap_result.prescribed == 0, (
            f"Expected prescribed=0 at hard cap (no re-prescription), got: {cap_result}"
        )

        row_at_cap = _read_uow_row(conn, uow_id)
        assert row_at_cap["status"] == "blocked", (
            f"Expected status=blocked after hard cap surface, "
            f"got: {row_at_cap['status']}"
        )
        # steward_cycles does NOT increment — the Steward surfaces, not prescribes
        assert row_at_cap["steward_cycles"] == _HARD_CAP_CYCLES, (
            f"steward_cycles must NOT increment when surfacing to Dan, "
            f"expected {_HARD_CAP_CYCLES}, got: {row_at_cap['steward_cycles']}"
        )

        # Exactly one surface notification to Dan
        assert len(dan_calls) == 1, f"Expected 1 Dan notification, got: {dan_calls}"
        assert dan_calls[0][1] == "hard_cap", (
            f"Expected condition='hard_cap', got: {dan_calls[0][1]}"
        )

    def test_early_warning_fires_at_cycle_4(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        Early-warning notification fires when steward_cycles reaches _EARLY_WARNING_CYCLES (4).

        The early-warning fires at the end of the Steward cycle that produces
        prescription #4 (i.e. when new_cycles == 4).
        """
        dan_calls: list[tuple] = []
        early_warning_calls: list[tuple] = []

        def mock_notify_dan(uow, condition, surface_log=None, return_reason=None):
            dan_calls.append((uow.id, condition))

        def mock_notify_dan_early_warning(uow, return_reason, new_cycles=None):
            early_warning_calls.append((uow.id, new_cycles))

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7004,
            title="Test early-warning at cycle 4",
            success_criteria="Early warning integration test.",
        )

        # Run 3 fail cycles without triggering early warning
        for cycle_num in range(1, _EARLY_WARNING_CYCLES):
            run_steward_cycle(
                registry=registry,
                github_client=_stub_github_client,
                artifact_dir=artifact_dir,
                notify_dan=mock_notify_dan,
                notify_dan_early_warning=mock_notify_dan_early_warning,
                bootup_candidate_gate=False,
            )
            _simulate_executor_fail(
                registry, conn, uow_id, tmp_path, f"failure #{cycle_num}"
            )

        # Cycle 3 done, no early warnings yet
        assert len(early_warning_calls) == 0, (
            f"No early warning before cycle 4, got: {early_warning_calls}"
        )

        # 4th Steward cycle — new_cycles will be 4 → early warning fires
        run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=mock_notify_dan,
            notify_dan_early_warning=mock_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        assert len(early_warning_calls) == 1, (
            f"Expected 1 early-warning call after cycle 4, got: {early_warning_calls}"
        )
        assert early_warning_calls[0][1] == _EARLY_WARNING_CYCLES, (
            f"Expected new_cycles={_EARLY_WARNING_CYCLES}, "
            f"got: {early_warning_calls[0][1]}"
        )

        row = _read_uow_row(conn, uow_id)
        assert row["steward_cycles"] == _EARLY_WARNING_CYCLES
        assert row["status"] == "ready-for-executor"

    def test_full_represcription_to_cap_sequence(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        Full end-to-end: seed → 4 failure cycles → surface at cap.

        Asserts the complete steward_cycles sequence: 1, 2, 3, 4, surface@cap.
        Checks that status transitions are correct at each step.
        """
        dan_calls: list[tuple] = []
        early_warning_calls: list[tuple] = []

        def mock_notify_dan(uow, condition, surface_log=None, return_reason=None):
            dan_calls.append((uow.id, condition))

        def mock_notify_dan_early_warning(uow, return_reason, new_cycles=None):
            early_warning_calls.append((uow.id, new_cycles))

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7005,
            title="Test full represcription-to-cap sequence",
            success_criteria="Full cycle end-to-end test.",
        )

        # Cycles 1 through HARD_CAP: each should prescribe and fail.
        # The hard cap fires when the Steward reads steward_cycles >= _HARD_CAP_CYCLES,
        # so we run _HARD_CAP_CYCLES full fail cycles first.
        for cycle_num in range(1, _HARD_CAP_CYCLES + 1):
            steward_result = run_steward_cycle(
                registry=registry,
                github_client=_stub_github_client,
                artifact_dir=artifact_dir,
                notify_dan=mock_notify_dan,
                notify_dan_early_warning=mock_notify_dan_early_warning,
                bootup_candidate_gate=False,
            )

            row = _read_uow_row(conn, uow_id)
            assert row["status"] == "ready-for-executor", (
                f"Cycle {cycle_num}: expected ready-for-executor, got: {row['status']}"
            )
            assert row["steward_cycles"] == cycle_num, (
                f"Cycle {cycle_num}: expected steward_cycles={cycle_num}, "
                f"got: {row['steward_cycles']}"
            )
            assert steward_result.prescribed == 1
            assert steward_result.surfaced == 0

            _simulate_executor_fail(
                registry, conn, uow_id, tmp_path, f"failure #{cycle_num}"
            )

            row_after = _read_uow_row(conn, uow_id)
            assert row_after["status"] == "ready-for-steward", (
                f"Cycle {cycle_num}: after exec fail expected ready-for-steward, "
                f"got: {row_after['status']}"
            )

        # Verify early warning fired at cycle 4
        assert len(early_warning_calls) == 1
        assert early_warning_calls[0][1] == _EARLY_WARNING_CYCLES

        # Final Steward run: reads steward_cycles=5 >= 5 → surfaces, no prescription
        cap_result = run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=mock_notify_dan,
            notify_dan_early_warning=mock_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        assert cap_result.surfaced == 1
        assert cap_result.prescribed == 0

        row_capped = _read_uow_row(conn, uow_id)
        assert row_capped["status"] == "blocked"
        assert row_capped["steward_cycles"] == _HARD_CAP_CYCLES

        assert len(dan_calls) == 1
        assert dan_calls[0] == (uow_id, "hard_cap")

    def test_audit_log_records_represcription_events(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        Audit log contains steward_prescription entries for each re-prescription.

        Two prescription cycles → two steward_prescription audit events.
        """
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=7006,
            title="Test audit log records re-prescription",
            success_criteria="Audit trail is coherent.",
        )

        # Cycle 1: prescribe
        run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=lambda *a, **kw: None,
            bootup_candidate_gate=False,
        )

        # Executor fails
        _simulate_executor_fail(registry, conn, uow_id, tmp_path, "audit trail test fail")

        # Cycle 2: re-prescribe
        run_steward_cycle(
            registry=registry,
            github_client=_stub_github_client,
            artifact_dir=artifact_dir,
            notify_dan=lambda *a, **kw: None,
            bootup_candidate_gate=False,
        )

        audit = _read_audit_log(conn, uow_id)
        prescription_events = [
            e for e in audit if e.get("event") == "steward_prescription"
        ]

        assert len(prescription_events) >= 2, (
            f"Expected at least 2 steward_prescription audit events, "
            f"got {len(prescription_events)}: {[e['event'] for e in audit]}"
        )

        # Verify steward_cycles increments in the prescription audit entries
        cycles_in_prescriptions = [
            json.loads(e["note"] or "{}").get("steward_cycles")
            for e in prescription_events
            if e.get("note")
        ]
        # First prescription is cycle 1, second is cycle 2
        assert cycles_in_prescriptions[0] == 1, (
            f"First prescription audit should show steward_cycles=1, "
            f"got: {cycles_in_prescriptions}"
        )
        assert cycles_in_prescriptions[1] == 2, (
            f"Second prescription audit should show steward_cycles=2, "
            f"got: {cycles_in_prescriptions}"
        )
